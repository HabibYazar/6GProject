"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiRequestError, fetchAiStatus } from "@/lib/api";

type Status = "checking" | "ok" | "unconfigured" | "missing" | "unauthorized" | "offline";

const META: Record<Status, { label: string; dot: string; text: string; hint: string }> = {
  checking:     { label: "Kontrol ediliyor...", dot: "bg-slate-400",   text: "text-slate-400",   hint: "AI servis durumu sorgulaniyor" },
  ok:           { label: "AI hazir",            dot: "bg-emerald-500", text: "text-emerald-400", hint: "AI ozellikleri kullanilabilir" },
  unconfigured: { label: "AI kapali",           dot: "bg-amber-500",   text: "text-amber-400",   hint: "OPENROUTER_API_KEY tanimli degil - AI ozellikleri devre disi" },
  missing:      { label: "Endpoint yok",        dot: "bg-orange-500",  text: "text-orange-400",  hint: "Backend guncel degil (/ai/status bulunamadi - HTTP 404)" },
  unauthorized: { label: "Giris yapilmadi",     dot: "bg-slate-500",   text: "text-slate-400",   hint: "AI durumunu gormek icin giris yapin" },
  offline:      { label: "Backend yok",         dot: "bg-rose-600",    text: "text-rose-400",    hint: "Backend'e ulasilamiyor" },
};

export default function AiServiceStatus({ intervalMs = 15000 }: { intervalMs?: number }) {
  const [status, setStatus] = useState<Status>("checking");
  const alive = useRef(true);

  const check = useCallback(async () => {
    try {
      const data = await fetchAiStatus();
      if (!alive.current) return;
      setStatus(data.configured ? "ok" : "unconfigured");
    } catch (err) {
      if (!alive.current) return;
      if (err instanceof ApiRequestError) {
        if (err.status === 401 || err.status === 403) {
          setStatus("unauthorized");
        } else if (err.status === 404) {
          setStatus("missing");
        } else {
          setStatus("offline");
        }
      } else {
        setStatus("offline");
      }
    }
  }, []);

  useEffect(() => {
    alive.current = true;
    check();
    const id = setInterval(check, intervalMs);
    return () => {
      alive.current = false;
      clearInterval(id);
    };
  }, [check, intervalMs]);

  const m = META[status];

  return (
    <div
      role="status"
      aria-live="polite"
      title={m.hint}
      className={"inline-flex items-center gap-2 rounded-md border border-slate-700 bg-slate-900/60 px-2.5 py-1 text-xs " + m.text}
    >
      <span className={"h-2 w-2 rounded-full " + m.dot} aria-hidden />
      <span className="font-medium">AI:</span>
      <span>{m.label}</span>
    </div>
  );
}