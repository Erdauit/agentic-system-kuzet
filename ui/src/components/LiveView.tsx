import { useEffect, useRef, useState } from "react";

import { startWhep } from "../api/whep";

export function LiveView({ cameraId, onClose, closeLabel }: { cameraId: string; onClose: () => void; closeLabel: string }) {
  const ref = useRef<HTMLVideoElement>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stop: (() => void) | null = null;
    let cancelled = false;
    if (ref.current) {
      startWhep(cameraId, ref.current)
        .then((s) => (cancelled ? s() : (stop = s)))
        .catch((e: unknown) => setError(String(e)));
    }
    return () => {
      cancelled = true;
      stop?.();
    };
  }, [cameraId]);

  return (
    <div className="live">
      <video ref={ref} autoPlay muted playsInline />
      {error && <p className="alert">{error}</p>}
      <button type="button" onClick={onClose}>
        {closeLabel}
      </button>
    </div>
  );
}
