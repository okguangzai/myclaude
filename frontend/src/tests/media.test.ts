import { beforeEach, it, expect, vi } from "vitest";
import { wav, screenshot, recordVoice, uploadImage } from "../media";
import { api } from "../api";
vi.mock("../api", () => ({ api: vi.fn(), apiBase: "/api" }));
beforeEach(() => vi.clearAllMocks());
it("encodes recorded PCM as mono 16k WAV", async () => {
  const blob = wav(new Float32Array(48000).fill(0.5), 48000);
  const buffer = await new Promise<ArrayBuffer>((resolve) => {
    const r = new FileReader();
    r.onload = () => resolve(r.result as ArrayBuffer);
    r.readAsArrayBuffer(blob);
  });
  const view = new DataView(buffer);
  expect(view.getUint32(24, true)).toBe(16000);
  expect(view.getUint16(22, true)).toBe(1);
  expect(view.getUint32(40, true)).toBe(32000);
  expect(view.getInt16(44, true)).toBe(16383);
});
it("propagates microphone denial without sending audio", async () => {
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getUserMedia: vi
        .fn()
        .mockRejectedValue(
          new DOMException("permission denied", "NotAllowedError"),
        ),
    },
  });
  await expect(recordVoice(() => {})).rejects.toThrow("permission denied");
  expect(api).not.toHaveBeenCalled();
});
it("stops all screen tracks when capture fails", async () => {
  const stop = vi.fn();
  Object.defineProperty(navigator, "mediaDevices", {
    configurable: true,
    value: {
      getDisplayMedia: vi
        .fn()
        .mockResolvedValue({ getTracks: () => [{ stop }] }),
    },
  });
  vi.spyOn(HTMLMediaElement.prototype, "play").mockRejectedValueOnce(
    Error("capture failed"),
  );
  await expect(screenshot()).rejects.toThrow("capture failed");
  expect(stop).toHaveBeenCalledOnce();
  expect(api).not.toHaveBeenCalled();
});
it("uploads a multipart image and preserves server errors", async () => {
  vi.mocked(api).mockRejectedValueOnce(Error("图片损坏"));
  await expect(uploadImage(new Blob(["bad"]), "bad.png")).rejects.toThrow(
    "图片损坏",
  );
  const args = vi.mocked(api).mock.calls[0];
  expect(args[0]).toBe("/features/media/image");
  expect(args[1]?.body).toBeInstanceOf(FormData);
});
