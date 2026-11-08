"""A calibration timeout must not produce hidden extra RPCs or release its hold."""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import pytrio as trio  # type: ignore[import-untyped]
from minidsh_rl import billing_calibration as calibration


@dataclass
class Tokenizer:
    def apply_chat_template(self, conversation: object, **kwargs: object) -> list[int]:
        assert conversation is not None
        assert kwargs["enable_thinking"] is False
        return [1, 2]


@dataclass
class Sampler:
    root: Path
    fail: bool
    calls: int = 0

    def get_tokenizer(self) -> Tokenizer:
        return Tokenizer()

    async def sample_async(self, **kwargs: object) -> object:
        assert kwargs["num_samples"] == 1
        state = json.loads((self.root / "expansion-v2/budget.json").read_text())
        assert sum(x["status"] == "pending" for x in state["charges"].values()) == 1
        self.calls += 1
        if self.fail:
            raise TimeoutError("unknown submitted operation")
        from types import SimpleNamespace  # noqa: PLC0415 - SDK stand-in

        return SimpleNamespace(
            input_tokens=2,
            output_tokens=1,
            sequences=[SimpleNamespace(tokens=[3], logprobs=[-0.2], text="OK", stop_reason="stop")],
        )


def fixture(tmp_path: Path) -> Path:
    (tmp_path / "core-budget.json").write_text(
        json.dumps(
            {"schema_version": 1, "limit": "50", "spent": "31.38", "pending": {}, "settled": []}
        )
    )
    quote = tmp_path / "quote.json"
    quote.write_text(
        json.dumps(
            {
                "prices": {
                    "currency": "CNY",
                    "token_unit": "per_1m_tokens",
                    "items": [
                        {
                            "display_name": "Qwen/Qwen3.5-4B",
                            "prices": {
                                key: {"unit_price": value, "billing_unit": "per_1m_tokens"}
                                for key, value in [
                                    ("prefill", 149),
                                    ("sample", 454),
                                    ("train", 454),
                                ]
                            },
                        }
                    ],
                }
            }
        )
    )
    return quote


@pytest.mark.parametrize("fail", [False, True])
async def test_real_queue_or_timeout_never_resubmits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, fail: bool
) -> None:
    quote = fixture(tmp_path)
    sampler = Sampler(tmp_path, fail)

    class Service:
        def create_sampling_client(self, *, base_model: str) -> Sampler:
            assert base_model == "Qwen/Qwen3.5-4B"
            return sampler

    monkeypatch.setattr(calibration, "ROOT", tmp_path)
    monkeypatch.setattr(trio, "ServiceClient", Service)
    if fail:
        with pytest.raises(TimeoutError, match="unknown"):
            await calibration.calibrate(tmp_path / "probe", quote)
        assert sampler.calls == 1
        assert not (tmp_path / "probe/summary.json").exists()
    else:
        await calibration.calibrate(tmp_path / "probe", quote)
        assert sampler.calls == 16
        summary = json.loads((tmp_path / "probe/summary.json").read_text())
        assert summary["global_committed_upper_cny"] == "33.70"
        assert summary["coding_rollouts"] == 0


def test_changed_price_rejects_before_artifact_or_service(tmp_path: Path) -> None:
    quote = fixture(tmp_path)
    data = json.loads(quote.read_text())
    data["prices"]["items"][0]["prices"]["sample"]["unit_price"] = 999
    quote.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="quote changed"):
        calibration.prepare(tmp_path / "not-created", quote)
    assert not (tmp_path / "not-created").exists()
