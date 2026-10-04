"""Validated local attachments and server-side Aliyun NLS adapters."""

import array
import base64
import io
import os
import re
import secrets
import wave
from urllib.parse import urlsplit

from dotenv import dotenv_values
from PIL import Image, UnidentifiedImageError

from .request_policy import allow_cloud

MAX_UPLOAD = 5 * 1024 * 1024


class Media:
    def __init__(self, ext):
        self.ext, self.store = ext, ext.store
        self.folder = self.store.data / "media"
        self.folder.mkdir(exist_ok=True)

    def save(self, data, mime, name, **extra):
        key = secrets.token_hex(16)
        (self.folder / key).write_bytes(data)
        return self.ext.jobs.put(
            "media", key, {"mime": mime, "name": name[:120], "size": len(data), **extra}
        )

    def image(self, data, name="image.png"):
        if len(data) > MAX_UPLOAD:
            raise ValueError("图片不能超过 5 MB")
        try:
            with Image.open(io.BytesIO(data)) as im:
                if im.format not in {"PNG", "JPEG", "WEBP"} or im.width * im.height > 16000000:
                    raise ValueError("支持 1600 万像素以内 PNG/JPEG/WebP 图片")
                im.load()
                clean = im.convert("RGB")
                output = io.BytesIO()
                clean.save(output, "JPEG", quality=90)
                return self.save(
                    output.getvalue(), "image/jpeg", name, width=im.width, height=im.height
                )
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise ValueError("图片损坏或格式不支持") from exc

    def get(self, key):
        if not re.fullmatch(r"[a-f0-9]{32}", key):
            raise ValueError("附件 ID 无效")
        row = self.ext.jobs.record("media", key)
        if not row or not (self.folder / key).is_file():
            raise ValueError("附件不存在")
        return row, self.folder / key

    def image_content(self, key, provider="openai"):
        row, path = self.get(key)
        if not row["mime"].startswith("image/"):
            raise ValueError("对话附件必须是图片")
        data = base64.b64encode(path.read_bytes()).decode()
        if provider == "anthropic":
            return {
                "type": "image",
                "source": {"type": "base64", "media_type": row["mime"], "data": data},
            }
        return {"type": "image_url", "image_url": {"url": f"data:{row['mime']};base64,{data}"}}

    def env(self):
        return {
            **dotenv_values(self.store.root / ".env"),
            **self.store.runtime_environment,
            **os.environ,
        }

    def connection(self, service):
        env = self.env()
        appkey, token, url = (env.get(service + "_" + x, "") for x in ("APPKEY", "TOKEN", "URL"))
        if not all((appkey, token, url)):
            raise ValueError(
                f"请在服务端 .env 或运行时环境配置 {service}_URL、{service}_APPKEY、{service}_TOKEN"
            )
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "wss"}
            or not parsed.hostname
            or not re.fullmatch(r"nls-gateway-[a-z0-9-]+\.aliyuncs\.com", parsed.hostname)
        ):
            raise ValueError("云语音地址必须是阿里云 NLS HTTPS/WSS 网关")
        endpoint = "https://" + parsed.hostname + "/stream/v1/" + service.lower()
        allow_cloud(self.store.current, endpoint)
        self.store.extra_secrets.update((appkey, token))
        return endpoint, appkey, token, env

    @staticmethod
    def wav(data):
        if len(data) > MAX_UPLOAD:
            raise ValueError("音频不能超过 5 MB")
        try:
            with wave.open(io.BytesIO(data)) as audio:
                if (
                    audio.getnchannels() != 1
                    or audio.getsampwidth() != 2
                    or audio.getframerate() not in {8000, 16000}
                ):
                    raise ValueError("录音需要 8/16 kHz、16 bit、单声道 WAV")
                if not 0 < audio.getnframes() <= audio.getframerate() * 60:
                    raise ValueError("单次录音应为 0–60 秒")
                frames = audio.readframes(audio.getnframes())
                if len(frames) != audio.getnframes() * 2:
                    raise ValueError("WAV 音频不完整")
                return audio.getframerate(), frames
        except (wave.Error, EOFError) as exc:
            raise ValueError("需要 PCM 编码 WAV 文件") from exc

    async def transcribe(self, data):
        self.wav(data)
        cfg = self.store.current
        if cfg.speech_provider != "dashscope":
            try:
                result = await self.nls_transcribe(data)
                self.ext.jobs.put("speech_status", "ASR", {"provider": "nls", "ok": True})
                return result
            except ValueError:
                if cfg.speech_provider == "nls":
                    raise
        result = await self.dashscope("ASR", data)
        self.ext.jobs.put("speech_status", "ASR", {"provider": "dashscope", "ok": True})
        return result

    async def synthesize(self, text):
        if not 1 <= len(text.strip()) <= 3000:
            raise ValueError("朗读文本为 1–3000 字符")
        cfg = self.store.current
        if cfg.speech_provider != "dashscope":
            try:
                result = await self.nls_synthesize(text)
                self.ext.jobs.put("speech_status", "TTS", {"provider": "nls", "ok": True})
                return result
            except ValueError:
                if cfg.speech_provider == "nls":
                    raise
        frames = bytearray()
        for start in range(0, len(text), 300):
            part = await self.dashscope("TTS", text[start : start + 300])
            with wave.open(io.BytesIO(part)) as audio:
                frames.extend(audio.readframes(audio.getnframes()))
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            audio.writeframes(frames)
        result = self.save(buffer.getvalue(), "audio/wav", "speech.wav", provider="dashscope")
        self.ext.jobs.put("speech_status", "TTS", {"provider": "dashscope", "ok": True})
        return result

    async def dashscope(self, service, value):
        cfg = self.store.current
        host = urlsplit(cfg.base_url).hostname
        if host not in {"dashscope.aliyuncs.com", "dashscope-intl.aliyuncs.com"} or not cfg.api_key:
            raise ValueError("百炼语音需要有效的百炼 API Key 和对应区域地址；也可配置 NLS 凭据")
        base = "https://" + host
        allow_cloud(cfg, base)
        if service == "ASR":
            url = base + "/compatible-mode/v1/chat/completions"
            payload = {
                "model": cfg.asr_model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_audio",
                                "input_audio": {
                                    "data": "data:audio/wav;base64,"
                                    + base64.b64encode(value).decode()
                                },
                            }
                        ],
                    }
                ],
                "stream": False,
            }
        else:
            url = base + "/api/v1/services/aigc/multimodal-generation/generation"
            payload = {
                "model": cfg.tts_model,
                "input": {"text": value, "voice": cfg.tts_voice, "language_type": "Auto"},
            }
        async with self.ext.jobs.agent.provider.limiter.slot(cfg):
            response = await self.ext.client.post(
                url, headers={"Authorization": "Bearer " + cfg.api_key}, json=payload, timeout=90
            )
        if response.is_error:
            raise ValueError(
                f"百炼 {service} 返回 HTTP {response.status_code}；检查模型权限和额度（原 NLS Token 不可用时会尝试此服务）"
            )
        result = response.json()
        if service == "ASR":
            return result["choices"][0]["message"]["content"]
        audio_url = result.get("output", {}).get("audio", {}).get("url", "")
        parsed = urlsplit(audio_url)
        # NLS may return an HTTP OSS URL; fetch the same signed object over TLS.
        if parsed.scheme == "http" and (parsed.hostname or "").endswith(".aliyuncs.com"):
            parsed = parsed._replace(scheme="https")
            audio_url = parsed.geturl()
        if (
            parsed.scheme != "https"
            or not (parsed.hostname or "").endswith(".aliyuncs.com")
            or parsed.username
            or parsed.port not in {None, 443}
        ):
            raise ValueError("语音服务返回的音频地址不受信任")
        async with self.ext.client.stream(
            "GET", audio_url, timeout=60, follow_redirects=False
        ) as audio:
            if audio.is_error or audio.is_redirect:
                raise ValueError("合成音频下载失败")
            data = bytearray()
            async for chunk in audio.aiter_bytes():
                data.extend(chunk)
                if len(data) > 20 * 1024 * 1024:
                    raise ValueError("合成音频超过 20 MB")
        # Normalize provider WAV to the same 16 kHz mono format as browser recordings.
        with wave.open(io.BytesIO(data)) as wav:
            if wav.getnchannels() != 1 or wav.getsampwidth() != 2:
                raise ValueError("合成音频格式不支持")
            samples = array.array("h", wav.readframes(wav.getnframes()))
            rate = wav.getframerate()
        normalized = array.array(
            "h",
            (
                samples[min(int(i * rate / 16000), len(samples) - 1)]
                for i in range(int(len(samples) * 16000 / rate))
            ),
        )
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav:
            wav.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
            wav.writeframes(normalized.tobytes())
        return buffer.getvalue()

    async def nls_transcribe(self, data):
        sample_rate, _ = self.wav(data)
        url, appkey, token, _ = self.connection("ASR")
        async with self.ext.jobs.agent.provider.limiter.slot(self.store.current):
            response = await self.ext.client.post(
                url,
                params={
                    "appkey": appkey,
                    "format": "wav",
                    "sample_rate": sample_rate,
                    "enable_punctuation_prediction": "true",
                },
                headers={"X-NLS-Token": token, "Content-Type": "application/octet-stream"},
                content=data,
                timeout=90,
            )
        if response.is_error:
            raise ValueError(
                f"语音识别服务 HTTP {response.status_code}；检查服务端 Token 和项目权限"
            )
        value = response.json()
        if value.get("status") != 20000000:
            raise ValueError(
                f"语音识别失败（状态 {value.get('status')}）；检查 Token 有效期和服务开通状态"
            )
        return value.get("result", "")

    async def nls_synthesize(self, text):
        if not 1 <= len(text.strip()) <= 3000:
            raise ValueError("朗读文本为 1–3000 字符")
        url, appkey, token, env = self.connection("TTS")
        pcm = bytearray()
        rate = int(env.get("TTS_SAMPLE_RATE") or 16000)
        if rate not in {8000, 16000}:
            rate = 16000
        for start in range(0, len(text), 300):
            async with self.ext.jobs.agent.provider.limiter.slot(self.store.current):
                response = await self.ext.client.post(
                    url,
                    json={
                        "appkey": appkey,
                        "token": token,
                        "text": text[start : start + 300],
                        "format": "wav",
                        "sample_rate": rate,
                        "voice": env.get("TTS_VOICE") or "xiaoyun",
                        "speech_rate": int(env.get("TTS_SPEECH_RATE") or 0),
                    },
                    timeout=90,
                )
            if response.is_error or not response.content.startswith(b"RIFF"):
                raise ValueError(
                    f"语音合成失败（HTTP {response.status_code}）；检查服务端 Token、发音人和项目权限"
                )
            _, frames = self.wav(response.content)
            pcm.extend(frames)
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as audio:
            audio.setparams((1, 2, rate, 0, "NONE", "not compressed"))
            audio.writeframes(pcm)
        return self.save(buffer.getvalue(), "audio/wav", "speech.wav")
