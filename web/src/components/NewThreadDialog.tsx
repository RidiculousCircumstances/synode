"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, RefreshCw, X } from "lucide-react";
import { useRouter } from "next/navigation";
import { type FormEvent, useEffect, useState } from "react";

import { createThread } from "@/lib/api";
import type { RunMode } from "@/types";

export default function NewThreadDialog({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const router = useRouter();
  const queryClient = useQueryClient();
  const [message, setMessage] = useState("");
  const [title, setTitle] = useState("");
  const [workspace, setWorkspace] = useState("");
  const [provider, setProvider] = useState("ollama");
  const [mode, setMode] = useState<RunMode>("general");

  const mutation = useMutation({
    mutationFn: createThread,
    onSuccess: (detail) => {
      setMessage("");
      setTitle("");
      void queryClient.invalidateQueries({ queryKey: ["threads"] });
      void queryClient.invalidateQueries({ queryKey: ["runs"] });
      onClose();
      router.push(`/threads/${detail.thread.id}`);
    },
  });

  useEffect(() => {
    if (!open) {
      return;
    }
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, [open]);

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (!message.trim()) {
      return;
    }
    mutation.mutate({
      message: message.trim(),
      title: title.trim() || null,
      workspace: workspace.trim() || null,
      model_provider: provider.trim() || null,
      mode,
    });
  };

  if (!open) {
    return null;
  }

  return (
    <div className="modal-layer" role="dialog" aria-modal="true" aria-labelledby="new-thread-title">
      <button type="button" className="modal-backdrop" aria-label="Close dialog" onClick={onClose} />
      <section className="modal-panel">
        <header className="modal-header">
          <h2 id="new-thread-title">New thread</h2>
          <button type="button" className="icon-button" aria-label="Close dialog" onClick={onClose}>
            <X size={16} aria-hidden />
          </button>
        </header>
        <form className="composer-shell thread-modal-form" onSubmit={submit}>
          <label className="field field-task">
            <span>Message</span>
            <textarea
              value={message}
              onChange={(event) => setMessage(event.target.value)}
              placeholder="Describe the coding, analysis, research, or database task"
              rows={7}
              autoFocus
            />
          </label>
          <label className="field">
            <span>Title</span>
            <input value={title} onChange={(event) => setTitle(event.target.value)} placeholder="Optional" />
          </label>
          <div className="form-grid">
            <label className="field">
              <span>Mode</span>
              <select value={mode} onChange={(event) => setMode(event.target.value as RunMode)}>
                <option value="general">general</option>
                <option value="coding">coding</option>
              </select>
            </label>
            <label className="field">
              <span>Provider</span>
              <input value={provider} onChange={(event) => setProvider(event.target.value)} />
            </label>
          </div>
          <label className="field">
            <span>Workspace</span>
            <input
              value={workspace}
              onChange={(event) => setWorkspace(event.target.value)}
              placeholder="/workspace/project"
            />
          </label>
          {mutation.error ? <div className="error-line">{mutation.error.message}</div> : null}
          <footer className="modal-actions">
            <button type="button" className="secondary-button" onClick={onClose}>
              Cancel
            </button>
            <button className="primary-button" type="submit" disabled={mutation.isPending || !message.trim()}>
              {mutation.isPending ? <RefreshCw size={16} aria-hidden className="spin" /> : <Play size={16} aria-hidden />}
              <span>Run</span>
            </button>
          </footer>
        </form>
      </section>
    </div>
  );
}
