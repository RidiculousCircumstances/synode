from __future__ import annotations

import json

from synode.runtime.decisions import FilePatch, PatchProposal
from synode.validation.patches import (
    categorize_patch_validation_failure,
    extract_required_patch_symbols,
    normalize_patch_proposal,
    validate_patch_proposal,
)


def test_native_loop_exhaustion_is_contract_invalid() -> None:
    assert (
        categorize_patch_validation_failure(
            ["native loop exceeded 8 steps without valid coding_inspection payload"]
        )
        == "contract_invalid"
    )


def test_patch_proposal_validation_rejects_delegated_operator_question() -> None:
    proposal = PatchProposal(
        action="needs_operator",
        summary="Delegate work",
        operator_question="Please review the file, make the necessary changes, and run the tests.",
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "app.py", "sha256": "a" * 64, "content": "print('ok')\n"}],
        allowed_verification_commands=[["pytest", "-q"]],
    )

    assert any("needs_operator must ask one specific ambiguity question" in error for error in errors)


def test_patch_proposal_normalizer_aligns_unique_indented_old_text() -> None:
    content = "def total(rows):\n    for row in rows:\n        value += row.amount\n    return value\n"
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="0" * 64,
                old_text="for row in rows:\nvalue += row.amount",
                new_text="for row in rows:\n        value -= row.amount",
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    normalized = normalize_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
    )

    assert normalized.patches[0].expected_sha256 == "1" * 64
    assert normalized.patches[0].old_text == "    for row in rows:\n        value += row.amount"


def test_patch_proposal_normalizer_aligns_new_text_base_indent() -> None:
    content = (
        "def total(rows):\n"
        "    for row in rows:\n"
        "        if row.kind == 'refund':\n"
        "            continue\n"
        "        value += row.amount\n"
    )
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="0" * 64,
                old_text="        if row.kind == 'refund':\n            continue",
                new_text="if row.kind == 'refund':\n    value -= row.amount\n",
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    normalized = normalize_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
    )

    assert normalized.patches[0].new_text == (
        "        if row.kind == 'refund':\n"
        "            value -= row.amount\n"
    )


def test_patch_proposal_normalizer_expands_ambiguous_patch_inside_required_function() -> None:
    content = (
        "def net_revenue_by_month(rows):\n"
        "    for row in rows:\n"
        "        amount = Decimal(row['amount'])\n"
        "        totals[month] = amount\n"
        "    return totals\n\n"
        "def top_customers(rows):\n"
        "    for row in rows:\n"
        "        amount = Decimal(row['amount'])\n"
        "        totals[customer] = amount\n"
        "    return totals\n"
    )
    proposal = PatchProposal(
        summary="Patch customer totals.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="0" * 64,
                old_text="amount = Decimal(row['amount'])",
                new_text=(
                    "amount = Decimal(row['amount'])\n"
                    "        if row['type'] == 'refund':\n"
                    "            amount *= -1"
                ),
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    normalized = normalize_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        required_patch_symbols=["top_customers"],
    )

    assert normalized.patches[0].old_text.startswith("def top_customers")
    assert "def net_revenue_by_month" not in normalized.patches[0].old_text
    assert "if row['type'] == 'refund':" in normalized.patches[0].new_text
    assert not validate_patch_proposal(
        normalized,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        required_patch_symbols=["top_customers"],
    )


def test_required_patch_symbols_come_from_failing_assertions() -> None:
    packet = {
        "inspection": {
            "observed_failures": [
                "\n".join(
                    [
                        "    rows = load_orders('orders.csv')",
                        ">   assert net_revenue_by_month(rows) == {'2026-07': Decimal('-7.00')}",
                        ">   assert top_customers(rows, limit=2) == []",
                    ]
                )
            ]
        }
    }
    file_context = [
        {
            "path": "ledger_app/ledger.py",
            "sha256": "1" * 64,
            "content": (
                "def load_orders(path):\n    return []\n"
                "def net_revenue_by_month(rows):\n    return {}\n"
                "def top_customers(rows, limit=2):\n    return []\n"
            ),
        },
        {
            "path": "tests/test_ledger.py",
            "sha256": "2" * 64,
            "content": "def test_net_revenue_by_month(): pass\n",
        },
    ]

    assert extract_required_patch_symbols(packet, file_context) == [
        "net_revenue_by_month",
        "top_customers",
    ]


def test_required_patch_symbols_parse_escaped_tool_result_failures() -> None:
    packet = {
        "inspection": {
            "observed_failures": [
                json.dumps(
                    {
                        "tool_name": "native.verify",
                        "ok": False,
                        "output": {
                            "commands": [
                                {
                                    "stdout": "\n".join(
                                        [
                                            "FF",
                                            ">       assert net_revenue_by_month(rows) == {}",
                                            "FAILED tests/test_ledger.py::test_net_revenue_counts_refunds_against_monthly_total",
                                            ">       assert top_customers(rows, limit=2) == []",
                                            "FAILED tests/test_ledger.py::test_top_customers_counts_refunds_against_customer_total",
                                        ]
                                    )
                                }
                            ]
                        },
                    }
                )
            ]
        }
    }
    file_context = [
        {
            "path": "ledger_app/ledger.py",
            "sha256": "1" * 64,
            "content": (
                "def net_revenue_by_month(rows):\n    return {}\n"
                "def top_customers(rows, limit=2):\n    return []\n"
            ),
        }
    ]

    assert extract_required_patch_symbols(packet, file_context) == [
        "net_revenue_by_month",
        "top_customers",
    ]


def test_required_patch_symbols_prefer_repair_verification_failures() -> None:
    packet = {
        "inspection": {
            "observed_failures": [
                ">       assert net_revenue_by_month(rows) == {}",
                ">       assert top_customers(rows, limit=2) == []",
            ]
        },
        "failed_verification": {
            "output": {
                "commands": [
                    {
                        "stdout": "\n".join(
                            [
                                ".F",
                                ">       assert top_customers(rows, limit=2) == []",
                                "FAILED tests/test_ledger.py::test_top_customers_counts_refunds_against_customer_total",
                            ]
                        )
                    }
                ]
            }
        },
    }
    file_context = [
        {
            "path": "ledger_app/ledger.py",
            "sha256": "1" * 64,
            "content": (
                "def net_revenue_by_month(rows):\n    return {}\n"
                "def top_customers(rows, limit=2):\n    return []\n"
            ),
        }
    ]

    assert extract_required_patch_symbols(packet, file_context) == ["top_customers"]


def test_patch_proposal_validation_rejects_missing_required_symbol() -> None:
    content = (
        "def net_revenue_by_month(rows):\n    return {}\n\n"
        "def top_customers(rows):\n    return []\n"
    )
    proposal = PatchProposal(
        summary="Patch one function.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="def net_revenue_by_month(rows):\n    return {}",
                new_text="def net_revenue_by_month(rows):\n    return {'fixed': 1}",
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        required_patch_symbols=["net_revenue_by_month", "top_customers"],
    )

    assert "patch does not address source symbols named in failing assertions: top_customers" in errors


def test_patch_proposal_validation_counts_block_inside_required_symbol() -> None:
    content = (
        "def net_revenue_by_month(rows):\n"
        "    totals = {}\n"
        "    for row in rows:\n"
        "        amount = row['amount']\n"
        "        if row['type'] == 'refund':\n"
        "            continue\n"
        "    return totals\n\n"
        "def top_customers(rows):\n"
        "    return []\n"
    )
    proposal = PatchProposal(
        summary="Patch refund handling.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="        if row['type'] == 'refund':\n            continue",
                new_text="        if row['type'] == 'refund':\n            amount *= -1",
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        required_patch_symbols=["net_revenue_by_month", "top_customers"],
    )

    assert "patch does not address source symbols named in failing assertions: top_customers" in errors
    assert "net_revenue_by_month" not in errors[0]


def test_patch_proposal_validation_rejects_python_syntax_error() -> None:
    content = (
        "def net_revenue_by_month(rows):\n"
        "    totals = {}\n"
        "    for row in rows:\n"
        "        if row['type'] == 'refund':\n"
        "            continue\n"
        "        totals[row['month']] = row['amount']\n"
        "    return totals\n"
    )
    proposal = PatchProposal(
        summary="Patch refund handling.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="        if row['type'] == 'refund':\n            continue",
                new_text="        if row['type'] == 'refund':\n        amount = -row['amount']",
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        allowed_verification_commands=[["pytest", "-q"]],
    )

    assert any("patch result is not valid Python syntax in ledger.py" in error for error in errors)


def test_patch_proposal_validation_rejects_direct_dict_accumulator_decrement() -> None:
    content = (
        "from decimal import Decimal\n\n"
        "def net_revenue_by_month(rows):\n"
        "    totals: dict[str, Decimal] = {}\n"
        "    for row in rows:\n"
        "        month = row['date'][:7]\n"
        "        amount = Decimal(row['amount'])\n"
        "        if row['type'] == 'refund':\n"
        "            continue\n"
        "        totals[month] = totals.get(month, Decimal('0')) + amount\n"
        "    return totals\n"
    )
    proposal = PatchProposal(
        summary="Patch refunds.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text=(
                    "        if row['type'] == 'refund':\n"
                    "            continue\n"
                    "        totals[month] = totals.get(month, Decimal('0')) + amount"
                ),
                new_text=(
                    "        if row['type'] == 'refund':\n"
                    "            totals[month] -= amount\n"
                    "        else:\n"
                    "            totals[month] = totals.get(month, Decimal('0')) + amount"
                ),
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        allowed_verification_commands=[["pytest", "-q"]],
    )

    assert any("direct augmented assignment totals[...] -=" in error for error in errors)


def test_patch_proposal_validation_rejects_refund_continue_without_reduction() -> None:
    content = (
        "from decimal import Decimal\n\n"
        "def top_customers(rows):\n"
        "    totals: dict[str, Decimal] = {}\n"
        "    for row in rows:\n"
        "        customer = row['customer']\n"
        "        amount = Decimal(row['amount'])\n"
        "        totals[customer] = totals.get(customer, Decimal('0')) + amount\n"
        "    return totals\n"
    )
    proposal = PatchProposal(
        summary="Patch refunds.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="        totals[customer] = totals.get(customer, Decimal('0')) + amount",
                new_text=(
                    "        totals[customer] = totals.get(customer, Decimal('0')) + amount\n"
                    "        if row['type'] == 'refund':\n"
                    "            continue"
                ),
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        allowed_verification_commands=[["pytest", "-q"]],
    )

    assert any("keeps refund rows as continue without reducing amount" in error for error in errors)


def test_patch_proposal_validation_accepts_python_multi_patch_result() -> None:
    content = (
        "def net_revenue_by_month(rows):\n"
        "    total = 0\n"
        "    return total\n\n"
        "def top_customers(rows):\n"
        "    totals = {}\n"
        "    return totals\n"
    )
    proposal = PatchProposal(
        summary="Patch two functions.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="    total = 0\n    return total",
                new_text="    total = sum(row['amount'] for row in rows)\n    return total",
            ),
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="    totals = {}\n    return totals",
                new_text="    totals = {'ok': len(rows)}\n    return totals",
            ),
        ],
        verification_commands=[["pytest", "-q"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        allowed_verification_commands=[["pytest", "-q"]],
        required_patch_symbols=["net_revenue_by_month", "top_customers"],
    )

    assert errors == []


def test_patch_proposal_validation_rejects_unsafe_verification_command() -> None:
    content = "def total(rows):\n    return sum(row.amount for row in rows)\n"
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="return sum(row.amount for row in rows)",
                new_text="return sum(row.net_amount for row in rows)",
            )
        ],
        verification_commands=[["git", "add", "."]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
    )

    assert "verification command 0 is unsafe: ['git', 'add', '.']" in errors


def test_patch_proposal_validation_rejects_commands_outside_catalog() -> None:
    content = "def total(rows):\n    return sum(row.amount for row in rows)\n"
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="return sum(row.amount for row in rows)",
                new_text="return sum(row.net_amount for row in rows)",
            )
        ],
        verification_commands=[["python", "-m", "pytest"]],
    )

    errors = validate_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        allowed_verification_commands=[["pytest", "-q"]],
    )

    assert "verification command 0 is not in allowed command catalog: ['python', '-m', 'pytest']" in errors

