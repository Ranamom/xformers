# Copyright (c) Facebook, Inc. and its affiliates. All rights reserved.
#
# This source code is licensed under the BSD license found in the
# LICENSE file in the root directory of this source tree.

# Run with --omit-baselines to skip slow baselines.
# See other CLI arguments in benchmark_main_helper in utils.py.

import sys
from typing import Any, Dict, Type

import torch

import xformers.ops as xops
from xformers.attn_bias_utils import create_attn_bias
from xformers.benchmarks.utils import NotSupportedInputError, benchmark_main_helper2

min_run_time = 0.5
device = torch.device("cuda")


CASES = [
    dict(
        B=max(1, 2 ** (16 - i)),
        Mq=1,
        Mkv=2**i,
        Hq=16,
        Hkv=hkv,
        K=128,
        attn_bias_type=xops.fmha.attn_bias.BlockDiagonalCausalWithOffsetPaddedKeysMask,
    )
    for i in range(8, 18)
    for hkv in (1, 2)
]


class AttentionDecodingBase:
    OP: Any = None

    def __init__(
        self,
        B: int,
        Mq: int,
        Mkv: int,
        Hq: int,
        Hkv: int,
        K: int,
        bw: bool,
        attn_bias_type,
    ) -> None:
        dtype = torch.float16
        torch.manual_seed(10)
        self.sub_label = (
            f"B={B} Mq={Mq} Mkv={Mkv} Hq={Hq} Hkv={Hkv} K={K} TotalBytes="
            f"{((B * Mkv * Hkv * K * 2) + (B * Mq * Hq * K) + (B * Mq * Hq * K)) * 2}"
        )
        self.label = "attn_decoding"
        self.shapes = (B, Mq, Mkv, Hq, Hkv, K)

        assert Hkv <= Hq
        assert Hq % Hkv == 0
        self.q = torch.randn(
            [B, Mq, Hkv, Hq // Hkv, K], device="cuda", dtype=dtype, requires_grad=bw
        )
        self.k = torch.randn(
            [B, Mkv, Hkv, 1, K], device="cuda", dtype=dtype, requires_grad=bw
        ).expand(-1, -1, -1, Hq // Hkv, -1)
        self.v = torch.randn(
            [B, Mkv, Hkv, 1, K], device="cuda", dtype=dtype, requires_grad=bw
        ).expand(-1, -1, -1, Hq // Hkv, -1)

        if Hq == Hkv:
            self.q = self.q[:, :, :, 0]
            self.k = self.k[:, :, :, 0]
            self.v = self.v[:, :, :, 0]
        if Hkv == 1:
            self.q = self.q[:, :, 0]
            self.k = self.k[:, :, 0]
            self.v = self.v[:, :, 0]

        self.attn_bias = create_attn_bias(
            attn_bias_type,
            batch_size=B,
            num_heads=Hq,
            num_heads_groups=Hq // Hkv,
            q_len=Mq,
            kv_len=Mkv,
            dtype=dtype,
            device=device,
            requires_grad=False,
            fmt="BMHK",
            op=self.OP,
        )

        if isinstance(
            self.attn_bias,
            xops.fmha.attn_bias.BlockDiagonalCausalWithOffsetPaddedKeysMask,
        ):
            self.q = self.q.view(1, -1, *self.q.shape[2:])
            self.k = self.k.view(1, -1, *self.k.shape[2:])
            self.v = self.v.view(1, -1, *self.v.shape[2:])

        if hasattr(self.OP, "not_supported_reasons"):
            inp = xops.fmha.Inputs(
                query=self.q, key=self.k, value=self.v, attn_bias=self.attn_bias
            )
            not_supported_reasons = self.OP.not_supported_reasons(inp)
            if not_supported_reasons:
                raise NotSupportedInputError(not_supported_reasons)

    def fw(self) -> None:
        try:
            xops.memory_efficient_attention_forward(
                self.q, self.k, self.v, op=self.OP, attn_bias=self.attn_bias
            )
        except (RuntimeError, ValueError) as e:
            print(f"Runtime error: {e}")


class AttentionDecodingDecoder(AttentionDecodingBase):
    OP = xops.fmha.decoder.FwOp


class AttentionDecodingCUTLASS(AttentionDecodingBase):
    OP = xops.fmha.cutlass.FwOp


class AttentionDecodingCK(AttentionDecodingBase):
    OP = xops.fmha.ck.FwOp


class AttentionDecodingCKDecoder(AttentionDecodingBase):
    OP = xops.fmha.ck_decoder.FwOp


class AttentionDecodingSplitKV(AttentionDecodingBase):
    OP = xops.fmha.triton_splitk.FwOp


class AttentionDecodingCKSplitKV(AttentionDecodingBase):
    OP = xops.fmha.ck_splitk.FwOp


class AttentionDecodingPyTorchRepeat(AttentionDecodingBase):
    def fw(self) -> None:
        B, Mq, Mkv, Hq, Hkv, K = self.shapes
        scale = 1 / K**0.5
        q = self.q.reshape([B, Mq, -1, K]).permute(0, 2, 1, 3)
        k = self.k.reshape([B, Mkv, -1, K]).permute(0, 2, 1, 3)
        v = self.v.reshape([B, Mkv, -1, K]).permute(0, 2, 1, 3)
        attn = (q @ k.transpose(-1, -2)).softmax(-1) * scale
        return attn @ v


BENCHMARKS: Dict[str, Type[AttentionDecodingBase]] = {
    "pytorch": AttentionDecodingPyTorchRepeat,
}

if torch.version.cuda:
    BENCHMARKS["decoder"] = AttentionDecodingDecoder
    BENCHMARKS["cutlass"] = AttentionDecodingCUTLASS

if torch.version.hip:
    BENCHMARKS.update(
        {
            "ck": AttentionDecodingCK,
            "ck-decoder": AttentionDecodingCKDecoder,
            "ck_splitK": AttentionDecodingCKSplitKV,
        }
    )


if (sys.version_info.major, sys.version_info.minor) >= (3, 9):
    BENCHMARKS["triton_splitK"] = AttentionDecodingSplitKV

try:
    import flash_attn

    class AttentionDecodingFlashAttention(AttentionDecodingBase):
        def fw(self) -> None:
            q, k, v = self.q, self.k, self.v
            if q.ndim == 5:
                B, Mq, H1, H2, K = q.shape
                B, Mkv, H1, H2, K = k.shape
                q = q.reshape([B, Mq, H1 * H2, K])
                k = k[:, :, :, 0]
                v = v[:, :, :, 0]
            return flash_attn.flash_attn_func(q, k, v)

    BENCHMARKS[
        f"flash-attention@{flash_attn.__version__}"
    ] = AttentionDecodingFlashAttention
except ImportError:
    pass


benchmark_main_helper2(
    "attn_decoding",
    fw=True,
    cases=CASES,
    functions=BENCHMARKS,
    min_run_time=min_run_time,
)
