# Installation Guide

This guide covers different ways to install and use the ka9q-python package.

## For End Users

### From PyPI
```bash
pip install ka9q-python
```

The distribution name is `ka9q-python`; the import name is `ka9q`.

### From GitHub (development version)
```bash
pip install git+https://github.com/HamSCI/ka9q-python.git
```

### From Local Clone
```bash
git clone https://github.com/HamSCI/ka9q-python.git
cd ka9q-python
pip install .
```

## For Development

### Editable Install
```bash
git clone https://github.com/HamSCI/ka9q-python.git
cd ka9q-python
pip install -e .
```

This creates a symbolic link, so changes to the source code take effect immediately without reinstalling.

### With Development Dependencies
```bash
pip install -e ".[dev]"
```

This includes pytest and other testing tools.

### Optional Extras

`ka9q-python` defines three optional dependency groups:

| Extra | Adds | Needed for |
|-------|------|------------|
| `dev` | `pytest`, `pytest-cov` | running the test suite |
| `tui` | `textual` | the `ka9q tui` interactive terminal UI |
| `opus` | `opuslib` | decoding `OPUS` / `OPUS_VOIP` RTP payloads with `ka9q.stream.OpusDecoder` |

Combine as needed, e.g. `pip install -e ".[dev,opus]"`.

## For Use in Other Applications

### In requirements.txt
```
ka9q-python>=3.9.0
```

### In setup.py
```python
setup(
    name="your-app",
    install_requires=[
        "ka9q-python>=3.9.0",
    ],
)
```

### In pyproject.toml
```toml
[project]
dependencies = [
    "ka9q-python>=3.9.0",
]
```

## Usage After Installation

Once installed, import from anywhere:

```python
from ka9q import RadiodControl, discover_channels

# Create control instance
control = RadiodControl("radiod.local")

# Discover channels
channels = discover_channels("radiod.local")

# Create a channel
control.create_channel(
    ssrc=14074000,
    frequency_hz=14.074e6,
    preset="usb",
    sample_rate=12000
)
```

## Verify Installation

Test that the package is properly installed:

```bash
python3 -c "import ka9q; print(f'ka9q version {ka9q.__version__} installed successfully')"
```

Expected output (version will match whatever is installed):
```
ka9q version 3.15.0 installed successfully
```

After installation, the `ka9q` command-line tool is also on your `PATH`:

```bash
ka9q list bee1-hf-status.local
ka9q tui  bee1-hf-status.local
```

See [CLI_GUIDE.md](CLI_GUIDE.md) for the full command reference.

## Platform-Specific Notes

### Linux
Optional: Install avahi-utils for optimized mDNS resolution
```bash
sudo apt-get install avahi-utils
```

### macOS
No additional dependencies needed. Built-in `dns-sd` may be used for mDNS.

### Windows
Package works with Python's built-in networking. Use IP addresses instead of `.local` hostnames for best reliability.

## Dependencies

### Required
- Python 3.9+
- `numpy >= 1.24.0`

### Optional (for enhanced functionality)
- `textual` — required for `ka9q tui`. Install via the `[tui]` extra or `pip install textual` directly.
- `opuslib` — required to decode `OPUS` / `OPUS_VOIP` RTP payloads via `ka9q.stream.OpusDecoder`. Install via the `[opus]` extra or `pip install opuslib`. (Linear-PCM encodings — `S16LE/BE`, `F32LE/BE`, `F16LE/BE`, `MULAW`, `ALAW` — are decoded in pure NumPy and need no extra dependency.)
- `avahi-utils` (Linux) — faster mDNS hostname resolution
- `control` from ka9q-radio — fallback path for channel discovery

### Pinned to a specific ka9q-radio commit

`ka9q-python` tracks a pinned ka9q-radio commit to keep protocol constants in sync. Read it from Python:

```python
from ka9q.compat import KA9Q_RADIO_COMMIT
print(KA9Q_RADIO_COMMIT)
```

See the "ka9q-radio Compatibility" section of [../README.md](../README.md) for the drift-check workflow.

## Troubleshooting

### ImportError: No module named 'ka9q'

The package is not installed. Install it using one of the methods above.

### Permission denied errors

Use `--user` flag:
```bash
pip install --user ka9q
```

Or use a virtual environment:
```bash
python3 -m venv venv
source venv/bin/activate
pip install ka9q-python
```

### "numpy" not found

Install numpy first:
```bash
pip install numpy>=1.24.0
```

Or let pip handle it automatically when installing ka9q.

## Uninstall

```bash
pip uninstall ka9q
```

## Building Distribution Packages

### Build wheel and source distribution
```bash
pip install build
python3 -m build
# or, with uv:
uv build
```

This creates (filenames track the current version in `pyproject.toml`):
- `dist/ka9q_python-X.Y.Z-py3-none-any.whl` (wheel)
- `dist/ka9q_python-X.Y.Z.tar.gz` (source distribution)

### Upload to PyPI (maintainers only)
```bash
pip install twine
twine upload dist/*
```

## Development Workflow

```bash
# Clone and install in editable mode
git clone https://github.com/HamSCI/ka9q-python.git
cd ka9q-python
pip install -e ".[dev]"

# Make changes to code
# ...

# Run tests
pytest

# Changes take effect immediately (no reinstall needed)
python3 examples/discover_example.py
```

## Integration Examples

### In a Signal Recorder Application

```python
# your_app/recorder.py
from ka9q import RadiodControl

class SignalRecorder:
    def __init__(self, radiod_address):
        self.control = RadiodControl(radiod_address)
    
    def setup_channel(self, frequency_mhz):
        self.control.create_channel(
            ssrc=int(frequency_mhz * 1e6),
            frequency_hz=frequency_mhz * 1e6,
            preset="iq",
            sample_rate=16000
        )
```

### In a Radio Monitoring Application

```python
# your_app/monitor.py
from ka9q import discover_channels

def list_active_channels(radiod_address):
    channels = discover_channels(radiod_address)
    
    for ssrc, info in channels.items():
        print(f"{info.frequency/1e6:.3f} MHz - {info.preset}")
```

### In a Command-Line Tool

```python
# your_app/cli.py
#!/usr/bin/env python3
import sys
from ka9q import RadiodControl

def main():
    control = RadiodControl(sys.argv[1])
    # Your CLI logic here
    
if __name__ == '__main__':
    main()
```

## Support

- Documentation: See README.md and other docs in the repository
- Issues: https://github.com/HamSCI/ka9q-python/issues
- Examples: See `examples/` directory
