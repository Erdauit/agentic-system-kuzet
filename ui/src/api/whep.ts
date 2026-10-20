// Live-просмотр по WebRTC (WHEP). Сигнализация — через core-api, видео — напрямую с MediaMTX.

export async function startWhep(cameraId: string, video: HTMLVideoElement): Promise<() => void> {
  const pc = new RTCPeerConnection();
  pc.addTransceiver("video", { direction: "recvonly" });
  pc.ontrack = (e) => {
    video.srcObject = e.streams[0] ?? new MediaStream([e.track]);
  };
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  // Ждём сбора ICE-кандидатов (не дольше 2 с): MediaMTX принимает полный offer.
  await new Promise<void>((resolve) => {
    if (pc.iceGatheringState === "complete") return resolve();
    const t = window.setTimeout(resolve, 2000);
    pc.onicegatheringstatechange = () => {
      if (pc.iceGatheringState === "complete") {
        window.clearTimeout(t);
        resolve();
      }
    };
  });
  const resp = await fetch(`/api/v1/cameras/${cameraId}/whep`, {
    method: "POST",
    headers: { "Content-Type": "application/sdp", "x-qoima-request": "1" },
    body: pc.localDescription?.sdp ?? "",
    credentials: "same-origin",
  });
  if (resp.status !== 201) {
    pc.close();
    throw new Error(`live недоступен (HTTP ${resp.status})`);
  }
  const location = resp.headers.get("Location");
  await pc.setRemoteDescription({ type: "answer", sdp: await resp.text() });
  return () => {
    pc.close();
    if (location) void fetch(location, { method: "DELETE", headers: { "x-qoima-request": "1" }, credentials: "same-origin" });
  };
}
