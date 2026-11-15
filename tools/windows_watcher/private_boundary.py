"""Frontière passive de stockage Windows ; aucune création ou activation.

L'approbation humaine désigne des chemins déjà provisionnés. La référence ne
constitue ni une autorité anti-rollback ni une permission de connexion réseau.
Les anciennes interfaces de fixtures restent synthétiques, sans mode réel.
"""
import os
from pathlib import Path
import re
import stat
from types import MappingProxyType

from . import credential_acl


ROLES = {"store", "fence", "witness", "trial", "control"}
WORKSPACE = Path(__file__).resolve().parents[2]
MAX_NODES = 512


class BoundaryError(RuntimeError):
    pass


def _windows():
    return os.name == "nt"


def _path(value):
    if not isinstance(value, (str, Path)):
        raise BoundaryError("PRIVATE_LOCATION_REQUIRED")
    path = Path(value)
    if (not path.is_absolute() or ".." in path.parts
            or path.anchor.startswith("\\\\")
            or any(part.endswith((".", " ")) or "~" in part
                   or any(character in part for character in '<>:"|?*\x00')
                   or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL",
                       *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
                   for part in path.parts if part != path.anchor)):
        raise BoundaryError("PRIVATE_LOCATION_INVALID")
    return path


def _overlap(a, b):
    return a == b or a in b.parents or b in a.parents


class PrivateStorageBoundary:
    """Identités et ACL relues avant accès, sans exception pour l'outillage.

    Tous les rôles sont obligatoires, distincts et sans chemin par défaut.
    Seul control accepte des sous-dossiers de travail, eux aussi inspectés.
    Une anomalie ferme définitivement cet objet ; aucune réparation/reset.
    """

    def __init__(self, paths, *, approval_reference, publication_root):
        if (type(paths) is not dict or set(paths) != ROLES
                or not isinstance(approval_reference, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,95}", approval_reference)):
            raise BoundaryError("PRIVATE_APPROVAL_REQUIRED")
        self.paths = MappingProxyType({role: _path(value) for role, value in paths.items()})
        self.approval_reference = approval_reference
        self.publication_root = _path(publication_root)
        excluded = [WORKSPACE, self.publication_root]
        for key in ("TEMP", "TMP"):
            if os.environ.get(key):
                excluded.append(_path(os.environ[key]))
        blocked = {"temp", "tmp", ".git", ".codex", "worktrees", "newstelegramwatcher",
                   "telegram-once", "credentials"}
        for path in self.paths.values():
            if (any(part.casefold() in blocked or
                    part.casefold().startswith("newstelegramprivate-qualification-")
                    for part in path.parts)
                    or any(_overlap(path, other) for other in excluded)):
                raise BoundaryError("PRIVATE_LOCATION_FORBIDDEN")
        values = list(self.paths.values())
        if any(_overlap(a, b) for i, a in enumerate(values) for b in values[i+1:]):
            raise BoundaryError("PRIVATE_LOCATION_OVERLAP")
        self.failed = False
        self._identities = None
        self._principal = None
        self.verify()

    def verify(self, **locations):
        if self.failed:
            raise BoundaryError("PRIVATE_BOUNDARY_CLOSED")
        try:
            if not _windows():
                raise BoundaryError("PRIVATE_WINDOWS_REQUIRED")
            requested = []
            for role, path in locations.items():
                if role not in self.paths:
                    raise BoundaryError("PRIVATE_ROLE_INVALID")
                candidate = _path(path)
                root = self.paths[role]
                if candidate != root and not (role == "control" and root in candidate.parents):
                    raise BoundaryError("PRIVATE_LOCATION_MISMATCH")
                requested.append(candidate)
            nodes, targets = {}, []
            pending = list(self.paths.values())
            while pending:
                current = pending.pop()
                if current in targets:
                    continue
                targets.append(current)
                for node in (current, *current.parents):
                    if node in nodes:
                        continue
                    info = node.lstat()
                    if (stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400
                            or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                            or (stat.S_ISREG(info.st_mode) and info.st_nlink != 1)):
                        raise BoundaryError("PRIVATE_PATH_UNSAFE")
                    nodes[node] = info
                    if len(nodes) > MAX_NODES:
                        raise BoundaryError("PRIVATE_PATH_QUOTA")
                if stat.S_ISDIR(nodes[current].st_mode):
                    pending.extend(current.iterdir())
            if any(not stat.S_ISDIR(nodes[root].st_mode) for root in self.paths.values()):
                raise BoundaryError("PRIVATE_DIRECTORY_REQUIRED")
            if any(path not in nodes for path in requested):
                raise BoundaryError("PRIVATE_LOCATION_MISSING")
            identities = {node: (nodes[node].st_dev, nodes[node].st_ino)
                          for root in self.paths.values() for node in (root, *root.parents)}
            if (any(not device or not inode for device, inode in identities.values())
                    or self._identities is not None and identities != self._identities):
                raise BoundaryError("PRIVATE_DIRECTORY_REPLACED")
            paths = list(nodes)
            snapshot = credential_acl.inspect(paths)
            if self._principal is not None and snapshot["user"] != self._principal:
                raise BoundaryError("PRIVATE_ACCOUNT_CHANGED")
            rows = {os.path.normcase(row["path"]): row for row in snapshot["rows"]}
            if len(snapshot["rows"]) != len(paths) or set(rows) != {os.path.normcase(str(p)) for p in paths}:
                raise BoundaryError("PRIVATE_ACL_INCOMPLETE")
            if any(rows[os.path.normcase(str(root))].get("protected") is not True
                   for root in self.paths.values()):
                raise BoundaryError("PRIVATE_ACL_INHERITANCE")
            for target in targets:
                chain = [target, *target.parents]
                # Une ACE tiers inherit-only sur un dossier privé menacerait
                # les prochains journaux/markers, même avant leur création.
                trusted = {snapshot["user"], "S-1-5-18", "S-1-5-32-544"}
                if any(rule["allow"] and rule["sid"] not in trusted
                       for rule in rows[os.path.normcase(str(target))]["rules"]):
                    raise BoundaryError("PRIVATE_ACL_EXTRA_GROUP")
                credential_acl.validate({"user": snapshot["user"], "rows": [
                    rows[os.path.normcase(str(node))] for node in chain]}, chain)
            self._identities = identities
            self._principal = snapshot["user"]
        except Exception:
            self.failed = True
            raise BoundaryError("PRIVATE_STORAGE_UNSAFE") from None


def check(boundary, *, create=False, **locations):
    if boundary is None:
        return  # Interfaces historiques réservées aux fixtures, sans transport réel.
    if type(boundary) is not PrivateStorageBoundary or create:
        raise BoundaryError("PRIVATE_PROVISIONING_REQUIRED")
    boundary.verify(**locations)


def shared(*resources):
    boundaries = [getattr(resource, "boundary", None) for resource in resources]
    first = next((value for value in boundaries if value is not None), None)
    if first is not None and (type(first) is not PrivateStorageBoundary
                              or any(value is not first for value in boundaries)):
        raise BoundaryError("PRIVATE_BOUNDARY_MISMATCH")
    return first
