# Model weights

Forecasting weights used by Obol's local forecaster. They are data, not code: half-precision tensors in the
[safetensors](https://github.com/huggingface/safetensors) format, read by `_cascade.py` as a JSON header plus raw
arrays with `numpy.frombuffer`. Nothing in them is unpickled or executed.

| File | Size | SHA-256 |
|---|---|---|
| `cascade-4m.f16.safetensors` | 6.5 MB | `fa4b72fcf9414ac0ffb1525abbf4aa0bf33f22e1b7dc5c45e0f0668e41b93186` |
| `cascade-toktok-4m-v1.f16.safetensors` | 6.5 MB | `91bc3abaddccd5f8a4cb8038ac7fc52b787e6c324d6a97bd45d40769dba147d7` |

**License:** the weights are MIT licensed, and so is the data they were trained on. See `LICENSE` in this folder.
The rest of this repository is Apache-2.0.
