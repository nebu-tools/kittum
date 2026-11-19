// The project's one open draft (DESIGN.md, "Protocol"): the text the leads write until one of them
// publishes it. The views hand every change here; it is saved a moment after the typing stops,
// one save at a time, each naming the revision it was written on. When someone else saved in the
// meantime, nothing is overwritten: the lead loads the newer draft or keeps their own text in the
// editor. A draft is not a record of the review; only what is published is.
import { useRouter } from "@tanstack/react-router";
import { useCallback, useEffect, useRef, useState } from "react";

import type { ProtocolDraft, StoredProtocol } from "../../data/schema.ts";
import type { Workbench } from "../../data/workbench.ts";
import { run, WorkbenchError } from "../../data/workbench.ts";
import { failure } from "../../ui/Form.tsx";

/** How long the typing stops before a save. */
const QUIET_MS = 800;

type DraftState =
  | { readonly state: "loading" }
  /** No draft is open, and the lead has written nothing yet. */
  | { readonly state: "none" }
  | { readonly state: "saved" }
  | { readonly state: "saving" }
  | { readonly state: "unsaved"; readonly error: string | null }
  /** Someone else saved, published, or discarded the draft; `newer` is the draft now, if any. */
  | { readonly state: "conflict"; readonly newer: ProtocolDraft | null };

export interface Draft {
  readonly status: DraftState;
  /** The text the views open: the open draft's when the screen loads or the lead loads the
   * newer one; null to open the published version or a blank form. */
  readonly opened: { readonly source: string; readonly baseRevisionId: string | null } | null;
  /** Raised whenever `opened` replaces what the views hold, so they open it afresh. */
  readonly epoch: number;
  /** The text as the lead has it now, from the revision `baseRevisionId` if any. */
  readonly change: (source: string, baseRevisionId: string | null) => void;
  readonly retry: () => void;
  readonly loadNewer: () => void;
  /** Keep the lead's text in the editor, unsaved; the next save writes it over the newer draft. */
  readonly keepMine: () => void;
  /** Close the draft: its text went into a revision, or the lead drops it. */
  readonly discard: () => Promise<void>;
  /** Save what is pending, then publish the draft with the lead's approval. */
  readonly publish: (as: string, reason: string | null) => Promise<StoredProtocol>;
}

const isConflict = (e: unknown) => e instanceof WorkbenchError && e.reason === "conflict";

export function useDraft(workbench: Workbench, projectId: string): Draft {
  const router = useRouter();
  const [status, setStatus] = useState<DraftState>({ state: "loading" });
  const [opened, setOpened] = useState<Draft["opened"]>(null);
  const [epoch, setEpoch] = useState(0);
  // The saving loop's own state, read in callbacks that outlive a render.
  const revision = useRef<number | null>(null);
  const pending = useRef<{ source: string; base: string | null } | null>(null);
  const saving = useRef<Promise<void> | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const conflicted = useRef(false);

  /** After a 409: whether the draft moved on from revision `at`, and if so, stop saving and
   * offer the newer one. A publication refused for what the draft says is a 409 too, with the
   * draft where the lead left it. */
  const stale = useCallback(
    async (at: number | null) => {
      const newer = await run(workbench.openDraft(projectId)).catch(() => null);
      if (newer !== null && newer.revision === at) return false;
      conflicted.current = true;
      setStatus({ state: "conflict", newer });
      return true;
    },
    [workbench, projectId],
  );

  /** Save what is pending, and what the lead types while that save runs, one save at a time. */
  const save = useCallback((): Promise<void> => {
    window.clearTimeout(timer.current);
    if (saving.current) return saving.current;
    if (!pending.current || conflicted.current) return Promise.resolve();
    setStatus({ state: "saving" });
    const done = (async () => {
      try {
        for (let next = pending.current; next && !conflicted.current; next = pending.current) {
          let saved: ProtocolDraft;
          if (revision.current === null) {
            saved = await run(
              workbench.startDraft({ projectId, source: next.source, baseRevisionId: next.base }),
            );
            // Someone else opened a draft first: theirs is the one open.
            if (saved.source !== next.source) return void (await stale(null));
          } else {
            saved = await run(
              workbench.saveDraft({ projectId, revision: revision.current, source: next.source }),
            );
          }
          revision.current = saved.revision;
          if (pending.current === next) pending.current = null;
        }
        if (!conflicted.current) setStatus({ state: "saved" });
      } catch (e) {
        if (isConflict(e) && (await stale(revision.current))) return;
        setStatus({ state: "unsaved", error: failure(e, "Kittum could not save the draft.") });
        throw e;
      } finally {
        saving.current = null;
      }
    })();
    saving.current = done;
    return done;
  }, [workbench, projectId, stale]);

  useEffect(() => {
    let live = true;
    run(workbench.openDraft(projectId)).then(
      (found) => {
        if (!live) return;
        revision.current = found?.revision ?? null;
        setOpened(
          found ? { source: found.source, baseRevisionId: found.base_revision_id ?? null } : null,
        );
        setStatus(found ? { state: "saved" } : { state: "none" });
      },
      (e) =>
        live &&
        setStatus({ state: "unsaved", error: failure(e, "Kittum could not load the draft.") }),
    );
    return () => {
      live = false;
      // Leaving the screen saves what is left, without waiting for it.
      window.clearTimeout(timer.current);
      if (pending.current && !conflicted.current) void save().catch(() => undefined);
    };
  }, [workbench, projectId, save]);

  const change = useCallback(
    (source: string, base: string | null) => {
      pending.current = { source, base };
      if (conflicted.current) return;
      setStatus((s) => (s.state === "saving" ? s : { state: "unsaved", error: null }));
      window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => void save().catch(() => undefined), QUIET_MS);
    },
    [save],
  );

  const retry = useCallback(() => void save().catch(() => undefined), [save]);

  const loadNewer = useCallback(() => {
    if (status.state !== "conflict") return;
    const { newer } = status;
    conflicted.current = false;
    pending.current = null;
    revision.current = newer?.revision ?? null;
    setOpened(
      newer ? { source: newer.source, baseRevisionId: newer.base_revision_id ?? null } : null,
    );
    setEpoch((n) => n + 1);
    setStatus(newer ? { state: "saved" } : { state: "none" });
    // Without a draft it may have been published: the screen reads the version in force again.
    if (!newer) void router.invalidate();
  }, [status, router]);

  const keepMine = useCallback(() => {
    if (status.state !== "conflict") return;
    conflicted.current = false;
    revision.current = status.newer?.revision ?? null;
    setStatus({ state: "unsaved", error: null });
  }, [status]);

  /** Every save started has ended, and nothing waits to be saved. */
  const settle = async () => {
    window.clearTimeout(timer.current);
    while (saving.current || (pending.current && !conflicted.current)) await save();
    if (conflicted.current) {
      throw new WorkbenchError({ reason: "conflict", message: "The draft changed elsewhere." });
    }
  };

  const discard = async () => {
    pending.current = null;
    await settle();
    const at = revision.current;
    if (at !== null) {
      try {
        await run(workbench.discardDraft({ projectId, revision: at }));
      } catch (e) {
        if (isConflict(e)) await stale(at);
        throw e;
      }
    }
    revision.current = null;
    setStatus({ state: "none" });
  };

  const publish = async (as: string, reason: string | null) => {
    await settle();
    const at = revision.current;
    if (at === null) {
      throw new WorkbenchError({ reason: "invalid", message: "There is no draft to publish." });
    }
    try {
      const stored = await run(workbench.publishDraft({ projectId, as, revision: at, reason }));
      revision.current = null;
      setStatus({ state: "none" });
      return stored;
    } catch (e) {
      if (isConflict(e)) await stale(at);
      throw e;
    }
  };

  return { status, opened, epoch, change, retry, loadNewer, keepMine, discard, publish };
}
