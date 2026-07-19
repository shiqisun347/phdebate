from __future__ import annotations

import contextlib
import gc
import importlib
import math
import subprocess
import threading
import time
from array import array
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Protocol

from moss_realtime_gateway.config import OPENMOSS_UPSTREAM_REVISION, GatewaySettings
from moss_realtime_gateway.low_latency_bridge import (
    CanaryAudioChunk,
    CanaryStageTiming,
    OpenMossLowLatencyTextStreamBridge,
)

# CUDA may reserve part of the board address space and report less than a
# board's nominal capacity. Preflight uses nvidia-smi's nominal MiB value, so
# allow the observed RTX 3090 driver reservation at runtime; a nominal 23GiB
# device still remains below the production floor and is rejected by both
# preflight and this check.
GPU_TOTAL_MEMORY_TOLERANCE_BYTES = 512 * 1024 * 1024


def gpu_total_memory_meets_floor(total_bytes: int, minimum_gib: float) -> bool:
    required_bytes = int(minimum_gib * (1024**3))
    return total_bytes + GPU_TOTAL_MEMORY_TOLERANCE_BYTES >= required_bytes


class CanaryPcmPayload:
    __slots__ = (
        "pcm16",
        "source_stage",
        "source_stage_index",
        "source_stage_duration_ms",
        "decoder_stage_index",
        "decoder_yield_ms",
        "turn_elapsed_ms",
    )

    def __init__(self, pcm16: bytes, observation: CanaryAudioChunk) -> None:
        self.pcm16 = pcm16
        self.source_stage = observation.source_stage
        self.source_stage_index = observation.source_stage_index
        self.source_stage_duration_ms = observation.source_stage_duration_ms
        self.decoder_stage_index = observation.decoder_stage_index
        self.decoder_yield_ms = observation.decoder_yield_ms
        self.turn_elapsed_ms = observation.turn_elapsed_ms

    def __bool__(self) -> bool:
        return bool(self.pcm16)


class BackendTurn(Protocol):
    def push_text(self, text: str) -> Iterable[bytes]: ...

    def finish(self) -> Iterable[bytes]: ...

    def abort(self) -> None: ...

    def close(self) -> None: ...


class SynthesisBackend(Protocol):
    name: str
    upstream_revision: str

    def startup(self) -> None: ...

    def warmup(self, prompts: dict[str, Path], text: str) -> None: ...

    def open_turn(self, *, prompt_path: Path, user_text: str, initial_text: str) -> BackendTurn: ...

    def shutdown(self) -> None: ...


class DisabledBackend:
    name = "disabled"
    upstream_revision = OPENMOSS_UPSTREAM_REVISION

    def startup(self) -> None:
        raise RuntimeError("MOSS gateway is disabled; set MOSS_GATEWAY_BACKEND explicitly")

    def warmup(self, prompts: dict[str, Path], text: str) -> None:
        del prompts, text

    def open_turn(self, *, prompt_path: Path, user_text: str, initial_text: str) -> BackendTurn:
        del prompt_path, user_text, initial_text
        raise RuntimeError("MOSS gateway is disabled")

    def shutdown(self) -> None:
        return None


class FakeTurn:
    def __init__(self, backend: FakeBackend, initial_text: str) -> None:
        self.backend = backend
        self.closed = False
        self.aborted = False
        self.context_entered = True
        backend.context_entered.set()
        self._phase = 0
        self._initial = list(self._pcm_for(initial_text))

    def _pcm_for(self, text: str) -> Iterable[bytes]:
        if not text:
            return []
        sample_rate = self.backend.sample_rate
        samples = max(sample_rate // 50, min(sample_rate // 5, len(text) * sample_rate // 200))
        start = self._phase
        self._phase += samples
        tone = array(
            "h",
            (
                round(5_000 * math.sin(2 * math.pi * 220 * (start + offset) / sample_rate))
                for offset in range(samples)
            ),
        )
        return [tone.tobytes()]

    def initial_audio(self) -> Iterable[bytes]:
        return self._initial

    def push_text(self, text: str) -> Iterable[bytes]:
        if self.closed:
            raise RuntimeError("fake turn is closed")
        if self.backend.push_delay_seconds:
            time.sleep(self.backend.push_delay_seconds)
        return self._pcm_for(text)

    def finish(self) -> Iterable[bytes]:
        if self.backend.finish_block is not None:
            self.backend.finish_block.wait()
        if self.backend.finish_delay_seconds:
            time.sleep(self.backend.finish_delay_seconds)
        return self._pcm_for("结束")

    def abort(self) -> None:
        self.aborted = True
        self.backend.abort_called.set()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self.context_entered = False
        self.backend.context_exited.set()


class FakeBackend:
    name = "fake"
    upstream_revision = OPENMOSS_UPSTREAM_REVISION

    def __init__(
        self,
        *,
        sample_rate: int = 24_000,
        finish_delay_seconds: float = 0.0,
        push_delay_seconds: float = 0.0,
        finish_block: threading.Event | None = None,
    ) -> None:
        self.sample_rate = sample_rate
        self.finish_delay_seconds = finish_delay_seconds
        self.push_delay_seconds = push_delay_seconds
        self.finish_block = finish_block
        self.started = threading.Event()
        self.warmed = threading.Event()
        self.context_entered = threading.Event()
        self.context_exited = threading.Event()
        self.abort_called = threading.Event()
        self.shutdown_called = threading.Event()

    def startup(self) -> None:
        self.started.set()

    def warmup(self, prompts: dict[str, Path], text: str) -> None:
        if not prompts:
            raise RuntimeError("fake warmup requires fixed prompts")
        finish_block = self.finish_block
        self.finish_block = None
        try:
            turn = self.open_turn(prompt_path=next(iter(prompts.values())), user_text="普通话", initial_text=text)
            list(getattr(turn, "initial_audio")())
            list(turn.finish())
            turn.close()
        finally:
            self.finish_block = finish_block
        self.warmed.set()

    def open_turn(self, *, prompt_path: Path, user_text: str, initial_text: str) -> BackendTurn:
        del user_text
        if not prompt_path.is_file():
            raise RuntimeError("prompt disappeared before fake turn start")
        self.context_exited.clear()
        return FakeTurn(self, initial_text)

    def shutdown(self) -> None:
        self.shutdown_called.set()


class OpenMossTurn:
    def __init__(
        self,
        *,
        bridge: Any,
        codec_context: Any,
        initial_text: str,
        cancel_event: threading.Event | None = None,
    ) -> None:
        self.bridge = bridge
        self.codec_context = codec_context
        self.closed = False
        self.aborted = False
        self.cancel_event = cancel_event or threading.Event()
        self._initial_text = initial_text
        self._initial_consumed = False

    @staticmethod
    def _convert(chunks: Iterable[Any]) -> Iterable[Any]:
        import numpy as np

        for chunk in chunks:
            if isinstance(chunk, CanaryStageTiming):
                yield chunk
                continue
            observation = chunk if isinstance(chunk, CanaryAudioChunk) else None
            audio = observation.audio if observation is not None else chunk
            values = audio.detach().cpu().numpy() if hasattr(audio, "detach") else np.asarray(audio)
            pcm = np.clip(np.asarray(values, dtype=np.float32).reshape(-1) * 32768.0, -32768, 32767)
            payload = pcm.astype(np.int16).tobytes()
            if payload:
                yield CanaryPcmPayload(payload, observation) if observation is not None else payload

    def initial_audio(self) -> Iterable[bytes]:
        if self._initial_consumed or not self._initial_text:
            return []
        self._initial_consumed = True
        return self._convert(self.bridge.push_text_delta(self._initial_text))

    def push_text(self, text: str) -> Iterable[bytes]:
        if self.closed or self.aborted:
            return []
        return self._convert(self.bridge.push_text_delta(text))

    def finish(self) -> Iterable[bytes]:
        if self.closed or self.aborted:
            return []
        return self._convert(self.bridge.finish(drain_step=1))

    def abort(self) -> None:
        self.aborted = True
        self.cancel_event.set()
        # The stable inferencer is reused by the next turn. An old, already
        # closed turn must never route its abort through a bridge that now
        # observes the replacement turn's cancel event.
        if self.closed:
            return
        bridge_abort = getattr(self.bridge, "abort", None)
        if callable(bridge_abort):
            bridge_abort()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            bridge_close = getattr(self.bridge, "close", None)
            if callable(bridge_close):
                bridge_close()
        finally:
            self.codec_context.__exit__(None, None, None)


class OpenMossCudaBackend:
    """Adapter pinned to OpenMOSS/MOSS-TTS commit ad99ec5.

    All CUDA/model imports are lazy so lifecycle tests and disabled deployments
    do not require a GPU or an OpenMOSS checkout.
    """

    name = "openmoss"
    upstream_revision = OPENMOSS_UPSTREAM_REVISION

    def __init__(self, settings: GatewaySettings) -> None:
        self.settings = settings
        self.model: Any = None
        self.tokenizer: Any = None
        self.processor: Any = None
        self.codec: Any = None
        self.device: Any = None
        self.codec_device: Any = None
        self.gpu_total_memory_gb: float | None = None
        self.gpu_free_memory_at_start_gb: float | None = None
        self._prompt_tokens: dict[Path, Any] = {}
        self._realtime_model_class: Any = None
        self._cancellable_inference_class: Any = None
        self._realtime_inferencer: Any = None
        self._model_dtype: Any = None
        self._codec_encoder_device: str | None = None
        self._codec_quantizer_device: str | None = None
        self._codec_decoder_device: str | None = None
        self._codec_context_mode = "full_codec"
        self._automatic_gc_disabled = False
        self._warmup_text_tokens = 0
        self._dynamo_cache_size_limit_applied: int | None = None
        self._decoder_stream: Any = None

    @property
    def placement(self) -> dict[str, Any]:
        if self.settings.diagnostic_codec_encoder_offload:
            mode = "diagnostic_encoder_cpu_decoder_cuda"
        elif self.settings.diagnostic_codec_device:
            mode = "diagnostic_whole_codec_cpu"
        else:
            mode = "production_all_cuda"
        return {
            "mode": mode,
            "realtime_model": str(self.device) if self.model is not None else None,
            "codec_encoder": self._codec_encoder_device,
            "codec_quantizer": self._codec_quantizer_device,
            "codec_decoder": self._codec_decoder_device,
            "codec_streaming_context": self._codec_context_mode,
            "prompt_tokens_cached": len(self._prompt_tokens),
            "prompt_token_frames": {
                path.name: int(getattr(tokens, "shape", (0, 0))[-1])
                for path, tokens in self._prompt_tokens.items()
            },
            "warmup_text_tokens": self._warmup_text_tokens,
            "dynamo_cache_size_limit": self._dynamo_cache_size_limit_applied,
            "async_decoder_enabled": bool(self._decoder_stream is not None),
            "automatic_gc_disabled": self._automatic_gc_disabled,
        }

    @staticmethod
    def _parameter_list(module: Any, *, label: str) -> list[Any]:
        parameters = getattr(module, "parameters", None)
        if not callable(parameters):
            raise RuntimeError(f"fixed codec {label} does not expose parameters")
        values = list(parameters())
        if not values:
            raise RuntimeError(f"fixed codec {label} unexpectedly has no parameters")
        return values

    @staticmethod
    def _parameter_devices(parameters: list[Any]) -> set[str]:
        return {str(parameter.device) for parameter in parameters}

    def _load_realtime_model(self) -> None:
        if self.model is not None:
            return
        if self._realtime_model_class is None or self._model_dtype is None:
            raise RuntimeError("OpenMOSS Realtime model loader was not initialized")
        self.model = self._realtime_model_class.from_pretrained(
            self.settings.model_path,
            revision=self.settings.model_revision,
            attn_implementation=self.settings.attention_implementation,
            torch_dtype=self._model_dtype,
        ).to(self.device)
        self.model.eval()

    def _get_cancellable_inference_class(self, inference_base: type[Any]) -> type[Any]:
        """Return one cancellation-aware inference type for this backend lifecycle.

        Torch Dynamo guards on ``type(self)``. Defining this subclass inside every
        ``open_turn`` therefore invalidates the guard and recompiles the realtime
        path on each turn. Keep the subclass identity stable while retaining a
        distinct cancellation event for every turn.
        """

        cached = self._cancellable_inference_class
        if cached is not None:
            if not issubclass(cached, inference_base):
                raise RuntimeError("OpenMOSS inference base changed during backend lifecycle")
            return cached

        class CancellableInference(inference_base):
            fixed_repetition_window = 50
            audio_channel_pad = 1024

            def __init__(self, *args: Any, cancel_event: threading.Event, **kwargs: Any) -> None:
                self.cancel_event = cancel_event
                super().__init__(*args, **kwargs)

            @property
            def is_finished(self) -> bool:
                return self.cancel_event.is_set() or super().is_finished

            def step(
                self,
                text_token: Any,
                temperature: float = 0.8,
                top_p: float = 0.6,
                top_k: int = 30,
                do_sample: bool = True,
                repetition_penalty: float | None = 1.1,
                repetition_window: int | None = 50,
            ) -> Any:
                """Run one upstream step with a compile-stable repetition history.

                The pinned upstream implementation stacks every generated frame,
                so the local-transformer input grows from ``[B, 1, C]`` through
                ``[B, 50, C]`` and triggers a new Dynamo specialization for each
                shape.  Keep the same most-recent-50-token penalty semantics while
                presenting one fixed shape to the compiled local runner.
                """

                import numpy as np
                import torch

                if repetition_window != self.fixed_repetition_window:
                    raise ValueError(
                        "OpenMOSS realtime inference requires repetition_window=50 "
                        "for its fixed-shape history"
                    )

                with torch.inference_mode():
                    if self._last_audio_tokens is None or self.attention_mask is None:
                        raise ValueError("You must call prefill() before step().")
                    if self.is_finished:
                        return self._last_audio_tokens

                    batch_size = self._last_audio_tokens.shape[0]
                    if text_token is None:
                        text_tokens = [self.text_pad_id] * batch_size
                    elif isinstance(text_token, torch.Tensor):
                        text_tokens = text_token.detach().cpu().tolist()
                    elif isinstance(text_token, (list, tuple, np.ndarray)):
                        text_tokens = list(text_token)
                    else:
                        text_tokens = [int(text_token)]

                    if len(text_tokens) != batch_size:
                        raise ValueError(
                            "text_token batch size mismatch: "
                            f"got {len(text_tokens)}, expected {batch_size}."
                        )

                    device = self._last_audio_tokens.device
                    text_t = torch.tensor(text_tokens, device=device, dtype=torch.long)
                    step_ids = torch.cat(
                        [text_t[:, None, None], self._last_audio_tokens.unsqueeze(1)],
                        dim=2,
                    )
                    self.attention_mask = torch.cat(
                        [self.attention_mask, (~self._is_stopping).unsqueeze(-1)],
                        dim=-1,
                    )

                    outputs = self.model(
                        input_ids=step_ids,
                        attention_mask=self.attention_mask,
                        past_key_values=self.past_key_values,
                        use_cache=True,
                        return_dict=True,
                    )
                    self.past_key_values = outputs.past_key_values
                    backbone_hidden_states = outputs.last_hidden_state[:, -1:, :]

                    recent_tokens = self._generated_tokens[-self.fixed_repetition_window :]
                    recent_history = torch.stack(recent_tokens, dim=1)
                    padding = torch.full(
                        (
                            batch_size,
                            self.fixed_repetition_window - len(recent_tokens),
                            self._last_audio_tokens.shape[1],
                        ),
                        self.audio_channel_pad,
                        dtype=self._last_audio_tokens.dtype,
                        device=device,
                    )
                    history = torch.cat([padding, recent_history], dim=1)
                    audio_tokens = self.generate_local_transformer(
                        hidden_states=backbone_hidden_states,
                        temperature=temperature,
                        top_p=top_p,
                        top_k=top_k,
                        do_sample=do_sample,
                        repetition_penalty=repetition_penalty,
                        repetition_window=repetition_window,
                        generated_tokens=history,
                        gen_step=self.fixed_repetition_window,
                    )

                    self._generated_tokens.append(audio_tokens)
                    del self._generated_tokens[: -self.fixed_repetition_window]
                    self._last_audio_tokens = audio_tokens
                    self._is_stopping |= audio_tokens[:, 0] == self.audio_eos_token
                    self._step_idx += 1
                    return audio_tokens

            def apply_repetition_penalty(
                self,
                scores: Any,
                history_tokens: Any,
                penalty: float = 1.1,
                repetition_window: int | None = None,
            ) -> Any:
                """Penalize valid history IDs once while ignoring fixed-shape pads."""

                import torch

                if repetition_window != self.fixed_repetition_window:
                    raise ValueError(
                        "OpenMOSS realtime inference requires repetition_window=50 "
                        "for its fixed-shape history"
                    )
                scores_ = scores[:, 0, :]
                valid = history_tokens != self.audio_channel_pad
                safe_history = torch.where(
                    valid,
                    history_tokens,
                    torch.zeros_like(history_tokens),
                )
                seen_counts = torch.zeros_like(scores_, dtype=torch.int32)
                seen_counts.scatter_add_(
                    1,
                    safe_history,
                    valid.to(dtype=torch.int32),
                )
                seen = seen_counts > 0
                penalized = torch.where(
                    scores_ < 0,
                    scores_ * penalty,
                    scores_ / penalty,
                )
                scores_.copy_(torch.where(seen, penalized, scores_))
                return scores_

        self._cancellable_inference_class = CancellableInference
        return CancellableInference

    def _prepare_realtime_inferencer(
        self,
        inference_base: type[Any],
        cancel_event: threading.Event,
    ) -> Any:
        """Reuse the compiled inferencer while resetting all per-turn state."""

        inference_class = self._get_cancellable_inference_class(inference_base)
        if self._realtime_inferencer is None:
            self._realtime_inferencer = inference_class(
                self.model,
                self.tokenizer,
                max_length=self.settings.max_length,
                cancel_event=cancel_event,
            )
        elif type(self._realtime_inferencer) is not inference_class:
            raise RuntimeError("OpenMOSS inferencer type changed during backend lifecycle")
        else:
            self._realtime_inferencer.cancel_event = cancel_event
        self._realtime_inferencer.reset_generation_state(keep_cache=False)
        return self._realtime_inferencer

    def _load_codec(self, AutoModel: Any) -> None:
        self.codec = AutoModel.from_pretrained(
            self.settings.codec_model_path,
            revision=self.settings.codec_revision,
            trust_remote_code=True,
        ).eval().to(self.codec_device)
        device = str(self.codec_device)
        self._codec_encoder_device = device
        self._codec_quantizer_device = device
        self._codec_decoder_device = device

    def _activate_diagnostic_codec_encoder_offload(self) -> None:
        """Keep only the pinned codec decode graph on CUDA after prompt encoding.

        The fixed codec's ``decode`` implementation reads ``quantizer`` and
        ``decoder`` only.  Its root ``streaming`` context nevertheless traverses
        every child, including the encoder, so split placement uses the matching
        decoder-only streaming contexts instead of deleting or replacing modules.
        """

        if not self.settings.diagnostic_codec_encoder_offload:
            return
        if self.codec is None:
            raise RuntimeError("codec must be loaded before encoder offload")
        if len(self._prompt_tokens) != 8:
            raise RuntimeError("codec encoder offload requires eight distinct pre-encoded prompts")
        encoder = getattr(self.codec, "encoder", None)
        quantizer = getattr(self.codec, "quantizer", None)
        decoder = getattr(self.codec, "decoder", None)
        if encoder is None or quantizer is None or decoder is None:
            raise RuntimeError("fixed codec split layout is missing encoder, quantizer, or decoder")
        encoder_parameters = self._parameter_list(encoder, label="encoder")
        quantizer_parameters = self._parameter_list(quantizer, label="quantizer")
        decoder_parameters = self._parameter_list(decoder, label="decoder")
        encoder_ids = {id(parameter) for parameter in encoder_parameters}
        decode_ids = {id(parameter) for parameter in quantizer_parameters + decoder_parameters}
        if encoder_ids & decode_ids:
            raise RuntimeError("fixed codec encoder shares parameters with the decode graph")
        expected_cuda = str(self.codec_device)
        if self._parameter_devices(encoder_parameters) != {expected_cuda}:
            raise RuntimeError("fixed codec encoder was not fully resident on the decode CUDA device")
        if self._parameter_devices(quantizer_parameters + decoder_parameters) != {expected_cuda}:
            raise RuntimeError("fixed codec decode graph was not fully resident on CUDA before encoder offload")

        import torch

        encoder.to("cpu")
        with torch.cuda.device(self.device):
            torch.cuda.synchronize()
        gc.collect()
        with torch.cuda.device(self.device):
            torch.cuda.empty_cache()

        if self._parameter_devices(encoder_parameters) != {"cpu"}:
            raise RuntimeError("fixed codec encoder did not move completely to CPU")
        if self._parameter_devices(quantizer_parameters + decoder_parameters) != {expected_cuda}:
            raise RuntimeError("fixed codec decode graph moved unexpectedly during encoder offload")
        self._codec_encoder_device = "cpu"
        self._codec_quantizer_device = expected_cuda
        self._codec_decoder_device = expected_cuda
        self._codec_context_mode = "decoder_only"

    def _prepare_for_warmup(self, prompts: dict[str, Path]) -> None:
        if self.settings.diagnostic_codec_encoder_offload:
            if len(prompts) != 8 or len(set(prompts.values())) != 8:
                raise RuntimeError("codec encoder offload requires eight distinct fixed prompt files")
        for path in prompts.values():
            self._encode_prompt(path)
        if self.settings.diagnostic_codec_encoder_offload:
            self._activate_diagnostic_codec_encoder_offload()
            self._load_realtime_model()

    def _codec_streaming_context(self) -> Any:
        if not self.settings.diagnostic_codec_encoder_offload:
            return (
                self.codec.streaming(batch_size=1)
                if hasattr(self.codec, "streaming")
                else contextlib.nullcontext()
            )
        decoder = getattr(self.codec, "decoder", None)
        if decoder is None:
            raise RuntimeError("fixed codec decoder is unavailable for split streaming")
        stack = contextlib.ExitStack()
        for decoder_module in decoder:
            streaming = getattr(decoder_module, "streaming", None)
            if callable(streaming):
                stack.enter_context(streaming(batch_size=1))
        return stack

    def startup(self) -> None:
        import torch
        from transformers import AutoModel, AutoTokenizer

        try:
            import torch._dynamo as torch_dynamo
        except (ImportError, ModuleNotFoundError):
            torch_dynamo = getattr(torch, "_dynamo", None)

        dynamo_config = getattr(torch_dynamo, "config", None)
        if dynamo_config is not None:
            dynamo_config.cache_size_limit = self.settings.dynamo_cache_size_limit
            self._dynamo_cache_size_limit_applied = int(dynamo_config.cache_size_limit)

        module = importlib.import_module("mossttsrealtime")
        module_file = Path(str(module.__file__ or "")).resolve()
        checkout = self.settings.upstream_checkout.expanduser().resolve()
        package_root = (checkout / "moss_tts_realtime").resolve()
        if not module_file.is_relative_to(package_root):
            raise RuntimeError("mossttsrealtime was not imported from the fixed upstream checkout")
        try:
            actual_revision = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError("unable to verify the OpenMOSS upstream checkout revision") from exc
        if actual_revision != self.settings.upstream_revision:
            raise RuntimeError("OpenMOSS checkout revision does not match the fixed gateway revision")
        MossTTSRealtime = module.MossTTSRealtime
        MossTTSRealtimeProcessor = module.MossTTSRealtimeProcessor
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is required for the openmoss backend")
        self.device = torch.device(self.settings.device)
        self.codec_device = torch.device(
            self.settings.diagnostic_codec_device or self.settings.device
        )
        if self.settings.async_decoder_enabled:
            with torch.cuda.device(self.codec_device):
                self._decoder_stream = torch.cuda.Stream()
        with torch.cuda.device(self.device):
            free_bytes, total_bytes = torch.cuda.mem_get_info()
        self.gpu_total_memory_gb = total_bytes / (1024**3)
        self.gpu_free_memory_at_start_gb = free_bytes / (1024**3)
        if not gpu_total_memory_meets_floor(total_bytes, self.settings.minimum_gpu_total_memory_gb):
            raise RuntimeError("GPU total memory is below the fixed OpenMOSS deployment floor")
        if self.gpu_free_memory_at_start_gb < self.settings.minimum_gpu_free_memory_gb:
            raise RuntimeError("GPU free memory is below the isolated OpenMOSS startup floor")
        torch.set_float32_matmul_precision("high")
        torch.backends.cuda.matmul.allow_tf32 = True
        torch.backends.cudnn.allow_tf32 = True
        self.tokenizer = AutoTokenizer.from_pretrained(
            self.settings.tokenizer_path,
            revision=self.settings.model_revision,
        )
        self.processor = MossTTSRealtimeProcessor(self.tokenizer)
        self._realtime_model_class = MossTTSRealtime
        self._model_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
        if self.settings.diagnostic_codec_encoder_offload:
            # Diagnostic 12GB ordering: full codec CUDA -> eight prompt encodes ->
            # encoder CPU offload -> BF16 Realtime model CUDA.  ``warmup`` owns
            # the last three steps because prompt paths are validated there.
            self._load_codec(AutoModel)
        else:
            self._load_realtime_model()
            self._load_codec(AutoModel)

    def _encode_prompt(self, path: Path) -> Any:
        import numpy as np
        import torch
        import torchaudio

        cached = self._prompt_tokens.get(path)
        if cached is not None:
            return cached
        waveform, sample_rate = torchaudio.load(path)
        if waveform.shape[0] > 1:
            waveform = waveform.mean(dim=0, keepdim=True)
        if sample_rate != 24_000:
            waveform = torchaudio.functional.resample(waveform, sample_rate, 24_000)
        # The pinned MOSS Audio Tokenizer accepts a batched tensor shaped
        # (batch, channels, samples).  Older upstream streaming helpers still
        # pass a Python list here, which is incompatible with the fixed codec
        # revision used by this gateway and fails before synthesis.
        source = waveform.unsqueeze(0).to(self.codec_device)
        with torch.inference_mode():
            encoded = self.codec.encode(
                source,
                chunk_duration=self.settings.prompt_chunk_duration_seconds,
            )
        if isinstance(encoded, dict):
            codes = encoded.get("codes_list", encoded.get("audio_codes"))
            if isinstance(codes, list):
                codes = codes[0]
        elif hasattr(encoded, "audio_codes"):
            codes = encoded.audio_codes
        elif hasattr(encoded, "codes_list"):
            codes = encoded.codes_list
            if isinstance(codes, list):
                codes = codes[0]
        elif isinstance(encoded, (tuple, list)) and encoded:
            codes = encoded[0]
        else:
            codes = None
        if codes is None:
            raise RuntimeError("codec encode response did not contain audio codes")
        if hasattr(codes, "detach"):
            codes = codes.detach().cpu().numpy()
        else:
            codes = np.asarray(codes)
        if getattr(codes, "ndim", 0) == 3:
            if codes.shape[1] == 1:
                codes = codes[:, 0, :]
            elif codes.shape[0] == 1:
                codes = codes[0]
            else:
                raise RuntimeError(f"unsupported prompt audio code shape: {tuple(codes.shape)}")
        if codes.ndim != 2:
            raise RuntimeError(f"unsupported prompt audio code shape: {tuple(codes.shape)}")
        self._prompt_tokens[path] = codes
        return codes

    def warmup(self, prompts: dict[str, Path], text: str) -> None:
        self._prepare_for_warmup(prompts)
        base_text = text.strip() or "现在开始普通话实时语音预热。"
        minimum_tokens = int(self.processor.delay_tokens_len) + self.settings.repetition_window + 1
        full_path_text = base_text
        full_path_tokens = self.tokenizer.encode(full_path_text, add_special_tokens=False)
        while len(full_path_tokens) < minimum_tokens:
            full_path_text = f"{full_path_text} {base_text}"
            full_path_tokens = self.tokenizer.encode(full_path_text, add_special_tokens=False)
        self._warmup_text_tokens = len(full_path_tokens)

        for voice_id, path in prompts.items():
            turn = self.open_turn(
                prompt_path=path,
                user_text=f"{voice_id} 普通话辩论预热",
                # Prefix length is voice-specific. Prime the text-token step
                # before, at and after the repetition-window boundary for every
                # fixed voice so its first real turn does not discover a new
                # compiled shape.
                initial_text=full_path_text,
            )
            try:
                chunks = [*getattr(turn, "initial_audio")(), *turn.finish()]
                if not chunks or not any(chunk for chunk in chunks):
                    raise RuntimeError(f"voice warmup returned no audio: {voice_id}")
            finally:
                turn.close()
        # PyTorch inference releases tensors by reference count. Python's
        # cyclic collector can otherwise scan the very large model object graph
        # in the middle of an active turn and create multi-second PCM gaps.
        # Keep it off only after warmup; shutdown restores the process default.
        if gc.isenabled():
            gc.disable()
            self._automatic_gc_disabled = True

    def open_turn(self, *, prompt_path: Path, user_text: str, initial_text: str) -> BackendTurn:
        import numpy as np

        torch: Any = None
        if self.settings.async_decoder_enabled:
            import torch as torch_module

            torch = torch_module
        from mossttsrealtime.streaming_mossttsrealtime import (
            AudioStreamDecoder,
            MossTTSRealtimeInference,
            MossTTSRealtimeStreamingSession,
            _sanitize_audio_tokens,
        )

        if self.model is None:
            raise RuntimeError("OpenMOSS Realtime model is not loaded")
        cancel_event = threading.Event()
        inferencer = self._prepare_realtime_inferencer(
            MossTTSRealtimeInference,
            cancel_event,
        )
        session = MossTTSRealtimeStreamingSession(
            inferencer,
            self.processor,
            codec=self.codec,
            codec_sample_rate=24_000,
            codec_encode_kwargs={"chunk_duration": self.settings.prompt_chunk_duration_seconds},
            prefill_text_len=self.processor.delay_tokens_len,
            temperature=self.settings.temperature,
            top_p=self.settings.top_p,
            top_k=self.settings.top_k,
            do_sample=self.settings.do_sample,
            repetition_penalty=self.settings.repetition_penalty,
            repetition_window=self.settings.repetition_window,
        )
        session.set_voice_prompt_tokens(self._encode_prompt(prompt_path))
        system_prompt = self.processor.make_ensemble(self._encode_prompt(prompt_path))
        instruction = user_text.strip() or "请使用普通话进行自然、清晰、稳定的辩论发言。"
        user_prompt_text = f"<|im_end|>\n<|im_start|>user\n{instruction}<|im_end|>\n<|im_start|>assistant\n"
        user_tokens = self.processor.tokenizer(user_prompt_text)["input_ids"]
        user_prompt = np.full(
            (len(user_tokens), self.processor.channels + 1),
            self.processor.audio_channel_pad,
            dtype=np.int64,
        )
        user_prompt[:, 0] = np.asarray(user_tokens, dtype=np.int64)
        session.reset_turn(
            input_ids=np.concatenate([system_prompt, user_prompt], axis=0),
            include_system_prompt=True,
            reset_cache=True,
        )
        decoder = AudioStreamDecoder(
            self.codec,
            chunk_frames=self.settings.decode_chunk_frames,
            overlap_frames=self.settings.decode_overlap_frames,
            initial_chunk_frames=self.settings.initial_chunk_frames,
            device=self.codec_device,
        )
        codec_context = self._codec_streaming_context()
        codec_context.__enter__()
        try:
            decoder_stream = self._decoder_stream

            def prepare_decode(tokens: Any) -> Any:
                if decoder_stream is None:
                    return None
                if torch is None:
                    raise RuntimeError("async decoder CUDA runtime is unavailable")
                ready = torch.cuda.Event(blocking=False, interprocess=False)
                ready.record(torch.cuda.current_stream(device=self.codec_device))

                def wait_until_ready() -> None:
                    decoder_stream.wait_event(ready)
                    record_stream = getattr(tokens, "record_stream", None)
                    if callable(record_stream):
                        record_stream(decoder_stream)

                return wait_until_ready

            def decoder_execution_context() -> Any:
                if decoder_stream is None:
                    return contextlib.nullcontext()
                if torch is None:
                    raise RuntimeError("async decoder CUDA runtime is unavailable")
                return torch.cuda.stream(decoder_stream)

            bridge = OpenMossLowLatencyTextStreamBridge(
                session,
                decoder,
                sanitize_audio_tokens=_sanitize_audio_tokens,
                batch_size=1,
                observe_stages=self.settings.canary_stage_observability,
                async_decoder=self.settings.async_decoder_enabled,
                decoder_queue_size=self.settings.async_decoder_queue_size,
                decoder_join_timeout_seconds=(
                    self.settings.async_decoder_join_timeout_seconds
                ),
                decoder_execution_context=decoder_execution_context,
                prepare_decode=prepare_decode,
            )
        except BaseException:
            codec_context.__exit__(None, None, None)
            raise
        return OpenMossTurn(
            bridge=bridge,
            codec_context=codec_context,
            initial_text=initial_text,
            cancel_event=cancel_event,
        )

    def shutdown(self) -> None:
        self._prompt_tokens.clear()
        self.model = None
        self.tokenizer = None
        self.processor = None
        self.codec = None
        self.device = None
        self.codec_device = None
        self.gpu_total_memory_gb = None
        self.gpu_free_memory_at_start_gb = None
        self._realtime_model_class = None
        self._cancellable_inference_class = None
        self._realtime_inferencer = None
        self._model_dtype = None
        self._codec_encoder_device = None
        self._codec_quantizer_device = None
        self._codec_decoder_device = None
        self._codec_context_mode = "full_codec"
        self._warmup_text_tokens = 0
        self._dynamo_cache_size_limit_applied = None
        decoder_stream = self._decoder_stream
        self._decoder_stream = None
        if decoder_stream is not None:
            decoder_stream.synchronize()
        if self._automatic_gc_disabled:
            gc.enable()
            self._automatic_gc_disabled = False
        gc.collect()
        try:
            import torch

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass


def build_backend(settings: GatewaySettings) -> SynthesisBackend:
    if settings.backend == "fake":
        return FakeBackend(sample_rate=settings.sample_rate)
    if settings.backend == "openmoss":
        return OpenMossCudaBackend(settings)
    return DisabledBackend()
