// WebSocket /api/v1/ws: обновления состояния камер и системных алертов. Переподключение — с паузой.

export type RealtimeMessage = { type: string; [key: string]: unknown };

export function connectRealtime(onMessage: (m: RealtimeMessage) => void): () => void {
  let socket: WebSocket | null = null;
  let closed = false;
  let timer: number | undefined;

  const open = () => {
    const proto = window.location.protocol === "https:" ? "wss" : "ws";
    socket = new WebSocket(`${proto}://${window.location.host}/api/v1/ws`);
    socket.onmessage = (e) => {
      try {
        onMessage(JSON.parse(String(e.data)) as RealtimeMessage);
      } catch {
        // повреждённое сообщение игнорируем
      }
    };
    socket.onclose = () => {
      if (!closed) timer = window.setTimeout(open, 3000);
    };
  };
  open();
  return () => {
    closed = true;
    window.clearTimeout(timer);
    socket?.close();
  };
}
