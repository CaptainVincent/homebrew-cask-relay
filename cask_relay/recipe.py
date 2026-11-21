"""Conservative comparison of already-reviewed cask recipe layouts."""

import re

# shortcut: one literal version and scalar/arm-intel SHA256 layout; review other layouts before extending.
_VERSION_LINE = re.compile(
    r"^[ \t]*version[ \t]+[\"'](?P<value>[0-9]+(?:\.[0-9]+)*)[\"'][ \t]*(?:#[^\n]*)?$"
)
_HASH_LINE = re.compile(
    r"^[ \t]*sha256[ \t]+(?:(?:arm|intel):[ \t]*)?[\"'](?P<value>[0-9a-f]{64})[\"'](?P<comma>,?)[ \t]*(?:#[^\n]*)?$"
)
_HASH_CONTINUATION = re.compile(
    r"^[ \t]*(?:arm|intel):[ \t]*[\"'](?P<value>[0-9a-f]{64})[\"'](?P<comma>,?)[ \t]*(?:#[^\n]*)?$"
)


VERSION = re.compile(r"[0-9]{1,18}(?:\.[0-9]{1,18}){0,7}")


def version_key(value):
    if not VERSION.fullmatch(value):
        raise ValueError("Unsupported numeric version")
    # Treat 1.2 and 1.2.0 as equal, not as an upgrade.
    pieces = tuple(map(int, value.split(".")))
    return pieces + (0,) * (8 - len(pieces))


def recipe_shape(text):
    if (
        "<<" in text
    ):  # shortcut: no heredoc/multiline literal support in the routine comparator.
        return None
    normal, versions, hashes, continuation = [], [], [], False
    for line in text.splitlines(keepends=True):
        version = _VERSION_LINE.fullmatch(line.rstrip("\n"))
        checksum = _HASH_LINE.fullmatch(line.rstrip("\n"))
        if checksum is None and continuation:
            checksum = _HASH_CONTINUATION.fullmatch(line.rstrip("\n"))
        if version:
            versions.append(version["value"])
            normal.append(
                line[: version.start("value")]
                + "<version>"
                + line[version.end("value") :]
            )
            continuation = False
        elif checksum:
            hashes.append(checksum["value"])
            normal.append(
                line[: checksum.start("value")]
                + "<sha256>"
                + line[checksum.end("value") :]
            )
            continuation = bool(checksum["comma"])
        else:
            normal.append(line)
            continuation = False
    if len(versions) != 1 or not VERSION.fullmatch(versions[0]) or not hashes:
        return None
    return "".join(normal), versions[0], hashes


def routine_recipe_change(before, after):
    if before == after:
        return True, "unchanged"
    old, new = recipe_shape(before), recipe_shape(after)
    if old is None or new is None or old[0] != new[0]:
        return False, "not strictly a version/checksum-only change"
    if version_key(new[1]) <= version_key(old[1]):
        return (
            False,
            "same-version checksum replacement, downgrade or version-format change",
        )
    return True, "strictly newer numeric version; other recipe bytes unchanged"
