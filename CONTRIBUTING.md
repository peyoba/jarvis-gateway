# Contributing to Jarvis Gateway

Thank you for your interest in contributing to **Jarvis Gateway**! We welcome bug reports, feature suggestions, documentation improvements, and pull requests.

## Development Setup

1. **Clone the repository**:
   ```bash
   git clone https://github.com/peyoba/jarvis-gateway.git
   cd jarvis-gateway
   ```

2. **Create a virtual environment**:
   ```bash
   python3 -m venv venv
   source venv/bin/activate
   pip install -e ".[dev]"
   ```

3. **Install Whisper dependency (macOS)**:
   ```bash
   brew install whisper.cpp
   # Download recommended GGML model
   mkdir -p ~/.cache/whisper
   curl -L -o ~/.cache/whisper/ggml-small-q5_1.bin \
     https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin
   ```

4. **Copy config template**:
   ```bash
   cp config.example.json config.json
   # Edit config.json with your LLM keys
   ```

## Running Tests

Run the test suite before submitting pull requests:
```bash
pytest
```

## Pull Request Guidelines

- Keep pull requests focused on a single change or feature.
- Ensure all tests pass.
- Maintain code formatting and type hints.
- Update documentation and `README.md` if changing configuration or API behavior.
