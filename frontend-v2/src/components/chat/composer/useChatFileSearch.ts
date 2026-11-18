import { useEffect, useRef, useState } from "react";
import { type ChatFile, type ChatFileStatus, fetchChatFiles } from "./chatFiles";

/** Waits this long after the last keystroke before asking the backend. */
export const FILE_SEARCH_DEBOUNCE_MS = 120;

/**
 * Files matching `query` while a mention is open (`query !== null`). Keeps
 * the last answer on screen while the next one loads, and drops answers
 * that arrive after a newer question.
 */
export function useChatFileSearch(agentId: string, query: string | null) {
  const [files, setFiles] = useState<ChatFile[]>([]);
  const [status, setStatus] = useState<ChatFileStatus>("idle");
  const seq = useRef(0);

  useEffect(() => {
    if (query === null) {
      seq.current += 1;
      setStatus("idle");
      setFiles([]);
      return;
    }
    const mine = ++seq.current;
    setStatus((s) => (s === "ready" ? s : "loading"));
    const id = setTimeout(() => {
      fetchChatFiles(agentId, query).then(
        (list) => {
          if (seq.current !== mine) return;
          setFiles(list.files);
          setStatus("ready");
        },
        (err: unknown) => {
          if (seq.current !== mine) return;
          setFiles([]);
          setStatus(err instanceof Error && err.message.includes("no_workspace") ? "no_folder" : "error");
        },
      );
    }, FILE_SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(id);
  }, [agentId, query]);

  return { files, status };
}
