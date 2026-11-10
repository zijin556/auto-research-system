"""Fetch an official npm KaTeX release, verify SHA512, copy static assets only."""
import base64
import hashlib
import io
import json
import tarfile
from pathlib import Path
from urllib.request import urlopen

VERSION = "0.19.1"
DEST = Path(__file__).resolve().parent.parent / "static" / "vendor" / "katex"


def main():
    with urlopen(f"https://registry.npmjs.org/katex/{VERSION}", timeout=40) as response:
        metadata = json.load(response)
    url = metadata["dist"]["tarball"]
    if not url.startswith("https://registry.npmjs.org/katex/-/"):
        raise ValueError("Unexpected package source")
    with urlopen(url, timeout=60) as response:
        data = response.read(12 * 1024 * 1024)
    digest = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
    if digest != metadata["dist"]["integrity"]:
        raise ValueError("Package integrity mismatch")
    count = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        for entry in archive.getmembers():
            if not entry.isfile():
                continue
            if entry.name == "package/LICENSE":
                relative = "LICENSE"
            elif entry.name.startswith("package/dist/"):
                relative = entry.name[len("package/dist/"):]
                if relative not in {"katex.min.js", "katex.min.css", "contrib/auto-render.min.js"} and not relative.startswith("fonts/"):
                    continue
            else:
                continue
            target = (DEST / relative).resolve()
            if not target.is_relative_to(DEST.resolve()):
                raise ValueError("Invalid package path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(archive.extractfile(entry).read())
            count += 1
    (DEST / "SOURCE.json").write_text(json.dumps({"package": "katex", "version": VERSION, "source": url, "integrity": digest, "documentation": "https://katex.org/docs/browser.html"}, indent=2), encoding="utf-8")
    print(f"Verified KaTeX {VERSION}: {count} local static files")


if __name__ == "__main__":
    main()
