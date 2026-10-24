"use client";

// X-Kit sheet (package C, concept P6 / §6.1): everything the operator needs
// to post a result himself — MC posts nothing, there is no X API.
//
// Previews (video 16:9, card 1080×1350), "Alles laden" (kit.zip) and single
// downloads as REAL links with a short-lived, resource-scoped download token
// (the server answers with Content-Disposition: attachment — the iOS "Save"
// path), copy buttons for the English public texts (facts, settings,
// alt text, link only once an episode is published), and the thread guide.
// Red gate = preview only, the reason and the fix ("Zahlen nachladen").
// A run cut at max_tokens stops the operator here BEFORE any download
// (private warning, never in the public texts).
//
// Phone: bottom sheet. Desktop ≥ 1024 px: wide dialog, previews left,
// texts and downloads right.
//
// Signature is frozen (XKitSheetProps).
import { useEffect, useId, useState } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Check, Copy, Download, Film, ImageIcon, Loader2, RefreshCw, X } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { FilePreview } from "@/components/task/FilePreview";
import { api } from "@/lib/api";
import { benchResultHref } from "@/lib/benchLinks";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";
import { notify } from "@/lib/notify";
// Legacy client only for the files-API subpath of the red-gate video preview.
import { benchApi } from "@/verticals/bench_studio/api";
import { benchW1Api, benchW1Keys } from "../api";
import { apiErrorText } from "../result/errors";
import { formatBytes, formatDay } from "../result/format";
import { visibleGateReasons } from "../result/GateView";
import { TruncationWarning } from "../result/TruncationWarning";
import { useTruncationAck } from "../result/truncationAck";
import { useRerunLarger } from "../result/useRerunLarger";
import { useBenchWords, type BenchWords } from "../result/words";
import type { BenchTestDetail, KitFile, KitFileName, XKit } from "../types";

export interface XKitSheetProps {
  testId: string;
  open: boolean;
  onClose: () => void;
}

export function XKitSheet({ testId, open, onClose }: XKitSheetProps) {
  const titleId = useId();
  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby={titleId} className="lg:max-w-[980px]">
      <div data-testid="bench-xkit-sheet" className="flex flex-col min-h-0 flex-1">
        <KitBody testId={testId} onClose={onClose} titleId={titleId} />
      </div>
    </ResponsiveModal>
  );
}

function fileOf(kit: XKit, name: KitFileName): KitFile | undefined {
  return kit.files.find((f) => f.name === name);
}

/** Tokens live a few minutes: renew a little before they run out. */
function renewMs(expiresIn: number): number {
  return Math.max(30, expiresIn - 30) * 1000;
}

function Watermark({ words }: { words: BenchWords }) {
  return (
    <div data-testid="kit-watermark" aria-hidden className="absolute inset-0 flex items-center justify-center pointer-events-none">
      <span
        className="text-sm font-semibold px-3 py-1 rounded-md"
        style={{
          color: STATUS_TEXT.error,
          border: `2px solid ${STATUS_TEXT.error}`,
          backgroundColor: alpha(C.bgDeep, 0.85),
          transform: "rotate(-8deg)",
        }}
      >
        {words.ts("gate.red")}
      </span>
    </div>
  );
}

function CopyBlock({
  testId,
  title,
  text,
  words,
  mono = false,
  children,
}: {
  testId: string;
  title: string;
  text: string;
  words: BenchWords;
  mono?: boolean;
  children: React.ReactNode;
}) {
  const [copied, setCopied] = useState(false);
  useEffect(() => {
    if (!copied) return;
    const id = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(id);
  }, [copied]);
  async function copy() {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
    } catch {
      notify.error(words.tr("kit.copyFailed"));
    }
  }
  return (
    <div data-testid={testId} className="flex flex-col gap-1 py-3" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {title}
        </h3>
        <button
          type="button"
          onClick={() => void copy()}
          aria-label={copied ? words.tr("kit.copied") : words.tr("kit.copy")}
          className="flex items-center gap-2 min-h-11 pl-3 rounded-md text-sm cursor-pointer"
          style={{ color: copied ? C.online : C.textSecondary }}
        >
          {copied ? <Check size={16} aria-hidden /> : <Copy size={16} aria-hidden />}
          <span aria-hidden>{copied ? words.tr("kit.copied") : words.tr("kit.copy")}</span>
        </button>
      </div>
      <div className={`text-sm break-words ${mono ? "font-mono text-xs" : ""}`} style={{ color: C.textSecondary }}>
        {children}
      </div>
    </div>
  );
}

function DownloadLink({ href, label }: { href: string; label: string }) {
  return (
    <a
      href={href}
      download
      className="flex items-center gap-2 min-h-11 pl-3 rounded-md text-sm"
      style={{ color: C.textPrimary }}
    >
      <Download size={16} aria-hidden />
      {label}
    </a>
  );
}

function fileMeta(f: KitFile | undefined, kind: string, words: BenchWords, extra?: string): string {
  const parts = [kind];
  if (f?.width && f?.height) parts.push(`${f.width}×${f.height}`);
  if (f?.duration_s) parts.push(words.tr("video.seconds", { s: new Intl.NumberFormat(words.locale).format(f.duration_s) }));
  if (f?.bytes && kind === "ZIP") parts.push(formatBytes(f.bytes, words.locale));
  if (extra) parts.push(extra);
  return parts.join(" · ");
}

function KitBody({ testId, onClose, titleId }: { testId: string; onClose: () => void; titleId: string }) {
  const words = useBenchWords();
  const t = words.tr;
  const router = useRouter();
  const qc = useQueryClient();
  const [acked, ack] = useTruncationAck(testId);

  const kitQ = useQuery({ queryKey: benchW1Keys.kit(testId), queryFn: () => benchW1Api.kit.get(testId) });
  const testQ = useQuery({ queryKey: benchW1Keys.test(testId), queryFn: () => benchW1Api.tests.get(testId) });
  const kit = kitQ.data;
  const test: BenchTestDetail | undefined = testQ.data;
  const downloadable = Boolean(kit?.downloadable);

  const tokenQ = useQuery({
    queryKey: [...benchW1Keys.kit(testId), "download-token"],
    queryFn: () => benchW1Api.kit.downloadToken(testId),
    enabled: downloadable,
    staleTime: (q) => (q.state.data ? renewMs(q.state.data.expires_in) : 0),
    refetchInterval: (q) => (q.state.data ? renewMs(q.state.data.expires_in) : false),
  });
  const token = tokenQ.data?.token ?? null;

  const harvest = useMutation({
    mutationFn: () => benchW1Api.tests.harvest(testId),
    onSuccess: (data) => {
      qc.setQueryData(benchW1Keys.test(testId), data);
      qc.invalidateQueries({ queryKey: benchW1Keys.kit(testId) });
      notify.success(t("notify.harvestStarted"));
    },
    onError: (err) => notify.error(apiErrorText(err, t("notify.harvestFailed"))),
  });

  const rerun = useRerunLarger(
    testId,
    (next) => {
      onClose();
      router.push(benchResultHref(next.id));
    },
    t("notify.rerunFailed"),
  );

  const header = (
    <div className="flex items-start justify-between gap-2 px-4 pt-3 lg:px-6 lg:pt-4">
      <div className="flex flex-col gap-1 min-w-0">
        <h2 id={titleId} className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {test ? t("kit.titleFor", { title: test.title, date: formatDay(test.created_at, words.locale) }) : t("kit.title")}
        </h2>
        <p className="text-xs" style={{ color: C.textMuted }}>
          {t("kit.lead")}
        </p>
      </div>
      <button
        type="button"
        onClick={onClose}
        aria-label={t("kit.close")}
        className="flex items-center justify-center w-11 h-11 -mr-2 rounded-md cursor-pointer shrink-0"
        style={{ color: C.textMuted }}
      >
        <X size={18} aria-hidden />
      </button>
    </div>
  );

  if (kitQ.isError) {
    return (
      <>
        {header}
        <p className="px-4 py-8 text-sm lg:px-6" style={{ color: C.textMuted }}>
          {t("kit.loadFailed")}
        </p>
      </>
    );
  }
  if (!kit) {
    return (
      <>
        {header}
        <p className="flex items-center gap-2 px-4 py-8 text-sm lg:px-6" style={{ color: C.textMuted }}>
          <Loader2 size={16} aria-hidden className="animate-spin" />
          {t("kit.loading")}
        </p>
      </>
    );
  }

  const truncation = kit.truncation ?? test?.truncation ?? null;
  const stop = Boolean(truncation) && !acked;
  const red = !downloadable;
  const redReasons = kit.gate.reasons.filter((r) => r.severity === "red");
  const amber = visibleGateReasons(kit.gate, true).filter((r) => r.severity === "amber");
  const canHarvest =
    (test ? test.allowed.includes("harvest") : true) && kit.gate.reasons.some((r) => r.fix === "harvest");
  const url = (name: KitFileName) => (token ? benchW1Api.kit.fileUrl(testId, name, token) : null);
  const video = fileOf(kit, "video.mp4");
  const card = fileOf(kit, "card.png");
  const zip = fileOf(kit, "kit.zip");
  const showDownloads = downloadable && !stop && Boolean(token);
  const videoUrl = downloadable && video?.ready ? url("video.mp4") : null;
  const cardUrl = downloadable && card?.ready ? url("card.png") : null;
  const zipUrl = zip?.ready ? url("kit.zip") : null;

  const notes = (
    <>
      {red && (
        <div
          data-testid="kit-blocked"
          role="status"
          className="flex items-start gap-2 rounded-lg p-3 text-sm"
          style={{ border: `1px solid ${alpha(C.error, 0.3)}`, backgroundColor: alpha(C.error, 0.06), color: C.textSecondary }}
        >
          <AlertTriangle size={16} aria-hidden className="shrink-0 mt-1" style={{ color: STATUS_TEXT.error }} />
          <span>
            {words.ts("gate.red")}:{" "}
            <span className="font-semibold" style={{ color: C.textPrimary }}>
              {redReasons.length
                ? redReasons.map((r) => words.gateReason(r)).join(" · ")
                : words.reason(kit.blocked_reason) ?? t("reason.unknown")}
            </span>
          </span>
        </div>
      )}
      {!red && stop && truncation && (
        <TruncationWarning
          truncation={truncation}
          contestants={test ? test.blocks.flatMap((b) => b.contestants) : []}
          words={words}
          canRerun={test ? test.allowed.includes("rerun_larger") : true}
          rerunPending={rerun.pending}
          costAsk={rerun.costAsk}
          onRerun={rerun.run}
          onContinue={ack}
        />
      )}
      {!red && amber.length > 0 && (
        <div
          data-testid="kit-notes"
          className="flex items-start gap-2 rounded-lg p-3 text-sm"
          style={{ border: `1px solid ${alpha(C.warning, 0.3)}`, color: C.textSecondary }}
        >
          <AlertTriangle size={16} aria-hidden className="shrink-0 mt-1" style={{ color: C.warning }} />
          <span>{t("kit.amberNote", { reasons: amber.map((r) => words.gateReason(r)).join(" · ") })}</span>
        </div>
      )}
    </>
  );

  const previews = (
    <div className="flex flex-col gap-4">
      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {t("kit.video")}
        </h3>
        <div className="relative w-full rounded-lg overflow-hidden" style={{ backgroundColor: C.bgDeep }}>
          {videoUrl ? (
            <video
              data-testid="kit-video"
              // "#t=0.1": WebKit paints the first frame instead of a black box.
              src={`${videoUrl}#t=0.1`}
              controls
              playsInline
              preload="metadata"
              className="w-full aspect-video block"
            />
          ) : test?.video ? (
            <FilePreview
              fileUrl={api.files.contentUrl("shared-deliverables", benchApi.sharedSubpath(test.video.path))}
              path={test.video.path}
              showDownload={false}
            />
          ) : (
            <div className="w-full aspect-video flex items-center justify-center gap-2 text-sm" style={{ color: C.textMuted }}>
              <Film size={16} aria-hidden />
              {t("kit.notReady")}
            </div>
          )}
          {red && (videoUrl || test?.video) && <Watermark words={words} />}
        </div>
        <div className="flex items-center justify-between gap-2 min-h-11">
          <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
            {fileMeta(video, "MP4", words)}
          </span>
          {showDownloads && videoUrl && <DownloadLink href={videoUrl} label={t("kit.saveVideo")} />}
        </div>
      </div>

      <div className="flex flex-col gap-2">
        <h3 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
          {t("kit.card")}
        </h3>
        <div
          className="relative w-full max-w-[300px] lg:max-w-[220px] aspect-[4/5] rounded-lg overflow-hidden"
          style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}` }}
        >
          {cardUrl ? (
            // eslint-disable-next-line @next/next/no-img-element -- token URL, no optimiser
            <img data-testid="kit-card" src={cardUrl} alt={kit.texts.alt_text} className="w-full h-full object-cover block" />
          ) : (
            <div className="w-full h-full flex flex-col items-center justify-center gap-2 p-4 text-center text-sm" style={{ color: C.textMuted }}>
              <ImageIcon size={18} aria-hidden />
              {t("kit.cardPending")}
            </div>
          )}
          {red && cardUrl && <Watermark words={words} />}
        </div>
        <div className="flex items-center justify-between gap-2 min-h-11">
          <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
            {fileMeta(card, "PNG", words, t("kit.portrait"))}
          </span>
          {showDownloads && cardUrl && <DownloadLink href={cardUrl} label={t("kit.saveCard")} />}
        </div>
        {kit.footer && (
          <span className="text-xs" style={{ color: C.textMuted }}>
            {t("kit.footer", { footer: kit.footer })}
          </span>
        )}
      </div>
    </div>
  );

  const texts = red ? (
    <p className="text-sm py-3" style={{ color: C.textSecondary, borderTop: `1px solid ${C.borderSubtle}` }}>
      {t("kit.redTexts")}
    </p>
  ) : (
    <div className="flex flex-col">
      <p className="text-xs pb-2" style={{ color: C.textMuted }}>
        {t("kit.englishNote")}
      </p>
      {kit.texts.facts.length > 0 && (
        <CopyBlock testId="copy-facts" title={t("kit.facts")} text={kit.texts.facts.join("\n")} words={words}>
          {kit.texts.facts.map((f) => (
            <p key={f}>{f}</p>
          ))}
        </CopyBlock>
      )}
      {kit.texts.settings.length > 0 && (
        <CopyBlock testId="copy-settings" title={t("kit.settings")} text={kit.texts.settings.join("\n")} words={words} mono>
          {kit.texts.settings.map((s) => (
            <p key={s}>{s}</p>
          ))}
        </CopyBlock>
      )}
      {kit.texts.alt_text && (
        <CopyBlock testId="copy-alt" title={t("kit.altText")} text={kit.texts.alt_text} words={words}>
          {kit.texts.alt_text}
        </CopyBlock>
      )}
      {kit.texts.link && (
        <CopyBlock testId="copy-link" title={t("kit.link")} text={kit.texts.link} words={words}>
          {kit.texts.link}
        </CopyBlock>
      )}
    </div>
  );

  let bottom: React.ReactNode = null;
  if (red && canHarvest) {
    bottom = (
      <button
        type="button"
        onClick={() => harvest.mutate()}
        disabled={harvest.isPending}
        className="flex items-center justify-center gap-2 w-full min-h-12 rounded-md text-base font-medium cursor-pointer disabled:cursor-wait"
        style={{ backgroundColor: C.accent, color: C.onAccent }}
      >
        {harvest.isPending ? <Loader2 size={18} aria-hidden className="animate-spin" /> : <RefreshCw size={18} aria-hidden />}
        {t("actions.harvest")}
      </button>
    );
  } else if (showDownloads && zipUrl) {
    bottom = (
      <a
        href={zipUrl}
        download
        className="flex items-center justify-center gap-2 w-full min-h-12 rounded-md text-base font-medium"
        style={{ backgroundColor: C.accent, color: C.onAccent }}
      >
        <Download size={18} aria-hidden />
        {t("kit.downloadAll")}
      </a>
    );
  } else if (downloadable && !stop && tokenQ.isError) {
    bottom = (
      <div className="flex items-center justify-between gap-2 text-sm" style={{ color: C.textMuted }}>
        {t("kit.tokenFailed")}
        <button type="button" onClick={() => void tokenQ.refetch()} className="min-h-11 px-3 rounded-md cursor-pointer" style={{ color: C.textPrimary }}>
          {t("kit.retry")}
        </button>
      </div>
    );
  }

  const thread = (
    <p data-testid="kit-thread" className="text-xs" style={{ color: C.textMuted }}>
      {t("kit.thread")}
    </p>
  );

  return (
    <>
      {header}
      <div className="flex-1 min-h-0 overflow-y-auto overscroll-contain px-4 pt-3 pb-6 lg:px-6" style={{ paddingBottom: "max(env(safe-area-inset-bottom), 24px)" }}>
        <div className="flex flex-col gap-3">{notes}</div>
        <div className="grid grid-cols-1 gap-6 mt-4 lg:grid-cols-[400px_minmax(0,1fr)] lg:gap-8">
          {previews}
          <div className="flex flex-col gap-4 min-w-0">
            {texts}
            {bottom}
            {thread}
          </div>
        </div>
      </div>
    </>
  );
}
