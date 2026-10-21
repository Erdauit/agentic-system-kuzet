// Предупреждение «камер больше, чем в плане мощности» (приёмка Фазы 3): показывается в мастере
// и в «Состоянии системы», пока план не пересчитан и не применён. Само ничего не меняет.

import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { CapacityStatus } from "../api/types";
import type { T } from "../i18n";

export function CapacityBanner({ t, refreshKey = 0 }: { t: T; refreshKey?: number }) {
  const [st, setSt] = useState<CapacityStatus | null>(null);
  useEffect(() => {
    api<CapacityStatus>("/api/v1/capacity/status").then(setSt).catch(() => setSt(null));
  }, [refreshKey]);
  if (!st || (!st.stale && st.draft?.verdict !== "does_not_fit")) return null;
  const overloaded = st.draft?.verdict === "does_not_fit";
  return (
    <div className={overloaded ? "alert" : "alert warn"} role="status">
      <strong>{t("cap.banner")}</strong>
      <ul>
        {st.messages.map((m) => <li key={m}>{m}</li>)}
        {st.changes.map((c) => (
          <li key={c.camera_id}>
            {c.camera_id}: {c.current ?? "—"} → {c.proposed} {t("cap.fps")}
          </li>
        ))}
      </ul>
      <a href="#/setup">{t("cap.open")}</a>
    </div>
  );
}
