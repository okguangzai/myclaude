"""A self-contained VS Code extension, packaged without build-time secrets."""

import io
import json
import zipfile
from pathlib import Path

MANIFEST = {
    "name": "myclaude",
    "displayName": "MyClaude",
    "publisher": "myclaude",
    "version": "0.8.0",
    "engines": {"vscode": "^1.85.0"},
    "main": "./extension.js",
    "activationEvents": ["onCommand:myclaude.ask"],
    "contributes": {
        "commands": [
            {"command": "myclaude.ask", "title": "MyClaude: Ask about selection"},
            {"command": "myclaude.configure", "title": "MyClaude: Configure connection"},
        ]
    },
}
SOURCE = Path(__file__).with_suffix(".js").read_text(encoding="utf-8")


def package():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("extension/package.json", json.dumps(MANIFEST))
        z.writestr("extension/extension.js", SOURCE)
        z.writestr(
            "[Content_Types].xml",
            '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="json" ContentType="application/json"/><Default Extension="js" ContentType="application/javascript"/><Default Extension="vsixmanifest" ContentType="text/xml"/></Types>',
        )
        z.writestr(
            "extension.vsixmanifest",
            '<PackageManifest Version="2.0.0" xmlns="http://schemas.microsoft.com/developer/vsx-schema/2011"><Metadata><Identity Language="en-US" Id="myclaude" Version="0.8.0" Publisher="myclaude"/><DisplayName>MyClaude</DisplayName><Description xml:space="preserve">MyClaude agent connection</Description></Metadata><Installation><InstallationTarget Id="Microsoft.VisualStudio.Code"/></Installation><Dependencies/><Assets><Asset Type="Microsoft.VisualStudio.Code.Manifest" Path="extension/package.json" Addressable="true"/></Assets></PackageManifest>',
        )
    return buf.getvalue()
