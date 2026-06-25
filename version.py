"""
POE2 Sentinel - Version Information

Single source of truth for application version.
Updated automatically by build_exe.py --bump [patch|minor|major]
"""

VERSION = "1.1.0"


def parse_version(version_str: str) -> tuple:
    """Parse version string to tuple."""
    return tuple(int(x) for x in version_str.split('.'))


def bump_version(version_str: str, bump_type: str) -> str:
    """
    Bump version string.
    
    Args:
        version_str: Current version (e.g., "1.0.0")
        bump_type: "patch", "minor", or "major"
    
    Returns:
        New version string
    """
    major, minor, patch = parse_version(version_str)
    
    if bump_type == "major":
        return f"{major + 1}.0.0"
    elif bump_type == "minor":
        return f"{major}.{minor + 1}.0"
    elif bump_type == "patch":
        return f"{major}.{minor}.{patch + 1}"
    else:
        raise ValueError(f"Invalid bump type: {bump_type}")
