import { api, apiBase } from "./api";

export async function uploadImage(file: Blob, name = "image.png") {
  const form = new FormData();
  form.append("file", file, name);
  return api("/features/media/image", { method: "POST", body: form });
}
export const mediaUrl = (id: string) =>
  `${apiBase}/features/media/${encodeURIComponent(id)}`;

export async function screenshot() {
  const stream = await navigator.mediaDevices.getDisplayMedia({
    video: true,
    audio: false,
  });
  try {
    const video = document.createElement("video");
    video.srcObject = stream;
    await video.play();
    const canvas = document.createElement("canvas");
    canvas.width = video.videoWidth;
    canvas.height = video.videoHeight;
    canvas.getContext("2d")!.drawImage(video, 0, 0);
    const blob = await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob(
        (b) => (b ? resolve(b) : reject(new Error("截图失败"))),
        "image/png",
      ),
    );
    return await uploadImage(blob, "screenshot.png");
  } finally {
    stream.getTracks().forEach((t) => t.stop());
  }
}

export function wav(samples: Float32Array, rate: number) {
  const ratio = rate / 16000,
    length = Math.floor(samples.length / ratio);
  const buffer = new ArrayBuffer(44 + length * 2),
    view = new DataView(buffer);
  const ascii = (at: number, s: string) =>
    [...s].forEach((c, i) => view.setUint8(at + i, c.charCodeAt(0)));
  ascii(0, "RIFF");
  view.setUint32(4, 36 + length * 2, true);
  ascii(8, "WAVE");
  ascii(12, "fmt ");
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 16000, true);
  view.setUint32(28, 32000, true);
  view.setUint16(32, 2, true);
  view.setUint16(34, 16, true);
  ascii(36, "data");
  view.setUint32(40, length * 2, true);
  for (let i = 0; i < length; i++) {
    const start = Math.floor(i * ratio),
      end = Math.min(
        samples.length,
        Math.max(start + 1, Math.floor((i + 1) * ratio)),
      );
    let sum = 0;
    for (let n = start; n < end; n++) sum += samples[n];
    const s = Math.max(-1, Math.min(1, sum / (end - start)));
    view.setInt16(44 + i * 2, s < 0 ? s * 32768 : s * 32767, true);
  }
  return new Blob([buffer], { type: "audio/wav" });
}

export async function recordVoice(onLimit: () => void) {
  const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  let ctx: AudioContext | undefined;
  try {
    ctx = new AudioContext();
    await ctx.resume();
    const source = ctx.createMediaStreamSource(stream),
      processor = ctx.createScriptProcessor(4096, 1, 1),
      chunks: Float32Array[] = [];
    const silence = ctx.createGain();
    silence.gain.value = 0;
    let count = 0,
      closed = false;
    processor.onaudioprocess = (e) => {
      if (closed) return;
      const remaining = ctx!.sampleRate * 60 - count;
      if (remaining <= 0) return;
      const chunk = new Float32Array(
        e.inputBuffer.getChannelData(0).slice(0, remaining),
      );
      chunks.push(chunk);
      count += chunk.length;
    };
    source.connect(processor);
    processor.connect(silence);
    silence.connect(ctx.destination);
    const timer = setTimeout(onLimit, 59000);
    async function finish(transcribe: boolean) {
      if (closed) return "";
      closed = true;
      clearTimeout(timer);
      processor.disconnect();
      source.disconnect();
      silence.disconnect();
      stream.getTracks().forEach((t) => t.stop());
      const rate = ctx!.sampleRate;
      await ctx!.close();
      if (!transcribe) return "";
      const joined = new Float32Array(count);
      let at = 0;
      for (const c of chunks) {
        joined.set(c, at);
        at += c.length;
      }
      const form = new FormData();
      form.append("file", wav(joined, rate), "recording.wav");
      return (
        await api("/features/speech/listen", { method: "POST", body: form })
      ).text;
    }
    return { stop: () => finish(true), cancel: () => finish(false) };
  } catch (e) {
    stream.getTracks().forEach((t) => t.stop());
    await ctx?.close();
    throw e;
  }
}

export function download(text: string, filename: string, type = "text/plain") {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

let installEvent: any;
window.addEventListener("beforeinstallprompt", (e: any) => {
  e.preventDefault();
  installEvent = e;
});
export async function installDesktop() {
  if (installEvent) {
    await installEvent.prompt();
    await installEvent.userChoice;
    installEvent = null;
  } else
    download(
      `[InternetShortcut]\nURL=${location.origin}${import.meta.env.BASE_URL}\n`,
      "MyClaude.url",
    );
}
