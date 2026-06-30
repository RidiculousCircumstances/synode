from __future__ import annotations

import re

from synode.schemas import RoleName

DATA_RE = re.compile(r"\b(data|csv|json|analy[sz]e|analysis|revenue|orders|данн|аналит|продаж)\b", re.I)
WEB_RE = re.compile(r"\b(web|internet|search|url|http|docs|latest|сеть|интернет|найди)\b", re.I)
DB_RE = re.compile(r"\b(db|database|postgres|sql|schema|table|бд|база)\b", re.I)
CODE_RE = re.compile(r"\b(code|repo|file|test|bug|fix|implement|код|тест|ошиб)\b", re.I)


def select_worker_roles(task: str) -> list[str]:
    roles: list[str] = []
    if CODE_RE.search(task):
        roles.append(RoleName.CODER.value)
    if DATA_RE.search(task):
        roles.append(RoleName.DATA_ANALYST.value)
    if WEB_RE.search(task):
        roles.append(RoleName.WEB_RESEARCHER.value)
    if DB_RE.search(task):
        roles.append(RoleName.DB_AGENT.value)
    if not roles:
        roles.append(RoleName.CODER.value)
    return roles

