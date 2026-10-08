"""Install only official tokenizer JSON data, never execute downloaded Python."""

import hashlib
import io
import os
import zipfile
from pathlib import Path
from urllib.request import urlopen

URL = "https://cdn.deepseek.com/api-docs/deepseek_v4_tokenizer.zip"


def main():
    destination = Path(os.environ.get(
        "DEEPSEEK_TOKENIZER_PATH", Path.home() / ".cache/jev-longseq/tokenizers/deepseek-v4.json"))
    with urlopen(URL, timeout=60) as response:
        archive = response.read(16_000_001)
    if len(archive) > 16_000_000:
        raise ValueError("Tokenizer download exceeds safety limit")
    with zipfile.ZipFile(io.BytesIO(archive)) as zipped:
        data = zipped.read("deepseek_v4_tokenizer/tokenizer.json")
    from tokenizers import Tokenizer

    Tokenizer.from_str(data.decode())  # Validate data before replacing the existing asset.
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(destination)
    print(f"Installed official V4 tokenizer: {destination}; sha256={hashlib.sha256(data).hexdigest()}")
    import tiktoken

    tiktoken.get_encoding("o200k_base")  # Warm the proxy's data cache on the execution host.


if __name__ == "__main__":
    main()
