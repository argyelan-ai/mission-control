"use client";

/**
 * Datei-Bytes mit Bearer-Header holen und als Objekt-URL zurueckgeben.
 *
 * Warum es diesen Umweg braucht: `<img src>` kann keinen Authorization-Header
 * mitschicken, und der Content-Endpunkt verlangt einen. Also holt der Hook die
 * Bytes per fetch und reicht eine blob:-URL weiter, die das Bild-Element
 * anzeigen kann.
 *
 * Lag frueher lokal in FilePreview.tsx; beim Bau der Chat-Anhang-Kacheln
 * (19.08.2026) wurde daraus ein geteilter Hook, statt die 35 Zeilen ein
 * zweites Mal zu schreiben. Die Objekt-URL wird beim Aufraeumen freigegeben —
 * ohne das leckt jede Vorschau Speicher, solange der Tab offen ist.
 */
import { useEffect, useState } from "react";
import { getToken } from "@/lib/api";

export function useAuthBlob(url: string | null): { blobUrl: string | null; loading: boolean; error: boolean } {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);

  useEffect(() => {
    if (!url) return;
    let active = true;
    let objectUrl: string | null = null;
    setLoading(true);
    setError(false);

    fetch(url, { headers: { Authorization: `Bearer ${getToken()}` } })
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status}`);
        return r.blob();
      })
      .then((blob) => {
        if (!active) return;
        objectUrl = URL.createObjectURL(blob);
        setBlobUrl(objectUrl);
        setLoading(false);
      })
      .catch(() => {
        if (!active) return;
        setError(true);
        setLoading(false);
      });

    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
      setBlobUrl(null);
    };
  }, [url]);

  return { blobUrl, loading, error };
}
