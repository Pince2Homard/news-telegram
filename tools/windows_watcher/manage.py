"""Installation locale explicite ; aucune tâche enregistrée ou activée."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from xml.etree import ElementTree as ET

if __package__:
    from . import watcher
else:
    import watcher

FILES = ("watcher.py", "manage.py", "config.example.json", "README.md", "PROTOCOL.md")


def checked_root(root):
    root = Path(root).absolute()
    if root.name != "NewsTelegramWatcher":
        raise watcher.WatcherError("Le dossier doit se nommer NewsTelegramWatcher.")
    for path in (root, root / "app"):
        if path.is_symlink() or path.is_junction():
            raise watcher.WatcherError("Installation via un lien/jonction refusée.")
    return root


def install(root, source=None):
    root = checked_root(root)
    source = Path(source) if source else Path(__file__).resolve().parent
    if source.resolve() == (root / "app").resolve():
        raise watcher.WatcherError("Mettre à jour depuis la copie de développement, pas depuis app.")
    # Tout vérifier avant la première copie. Configuration et curseurs existants
    # ne sont jamais remplacés pendant une installation/mise à jour.
    contents = {name: (source / name).read_bytes() for name in FILES}
    if (root / "config.json").exists():
        watcher.load_config(root / "config.json")
    app = root / "app"
    app.mkdir(parents=True, exist_ok=True)
    for name, content in contents.items():
        target = app / name
        if target.is_symlink() or target.is_junction():
            raise watcher.WatcherError("Fichier lié refusé.")
        target.write_bytes(content)
    if not (root / "config.json").exists():
        watcher.save_state(root / "config.json", watcher.default_config())
    watcher.save_state(root / "installation.json", {
        "python": str(Path(sys.executable).resolve()),
        "files": {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()},
    })
    return {"installed": str(app), "task_registered": False, "poll_started": False}


def diagnose(root):
    root = checked_root(root)
    issues, warnings = [], []
    if os.name != "nt":
        warnings.append("Windows requis pour les notifications ; mode hors ligne disponible.")
    if sys.version_info[:2] != (3, 12):
        issues.append("Python 3.12 requis, comme la CI.")
    if not shutil.which("powershell.exe"):
        warnings.append("PowerShell Windows absent : notifications indisponibles.")
    if not os.environ.get("SESSIONNAME") or os.environ.get("SESSIONNAME") == "Services":
        warnings.append("Session interactive non confirmée ; affichage à vérifier visuellement.")
    try:
        config = watcher.load_config(root / "config.json")
        state = watcher.load_state(root / "state.json")
        if state.get("repository", config["repository"]) != config["repository"]:
            issues.append("Le dépôt de la configuration diffère des curseurs.")
    except (watcher.WatcherError, OSError):
        issues.append("Configuration/état absent ou invalide ; aucun fichier réinitialisé.")
    try:
        manifest = json.loads((root / "installation.json").read_text(encoding="utf-8"))
        if not Path(manifest["python"]).is_file():
            issues.append("Interpréteur installé introuvable.")
        for name in FILES:
            digest = hashlib.sha256((root / "app" / name).read_bytes()).hexdigest()
            if digest != manifest["files"][name]:
                issues.append(f"Fichier installé différent : {name}")
    except (OSError, ValueError, KeyError, TypeError):
        issues.append("Installation absente ou manifeste incomplet.")
    return {"ok": not issues, "issues": issues, "warnings": warnings,
            "network_checked": False, "notification_checked": False,
            "task_activated": False}


def prepare_task(root):
    root = checked_root(root)
    result = diagnose(root)
    if not result["ok"]:
        raise watcher.WatcherError("Installation non vérifiée ; lancer check.")
    config = watcher.load_config(root / "config.json")
    manifest = json.loads((root / "installation.json").read_text(encoding="utf-8"))
    # L'identité est obtenue localement, jamais depuis un commentaire GitHub.
    user = os.environ.get("USERNAME", "")
    domain = os.environ.get("USERDOMAIN", "")
    if not user:
        raise watcher.WatcherError("Utilisateur Windows introuvable.")
    identity = f"{domain}\\{user}" if domain else user
    ns = "http://schemas.microsoft.com/windows/2004/02/mit/task"
    ET.register_namespace("", ns)
    task = ET.Element(f"{{{ns}}}Task", version="1.2")

    def add(parent, name, value=None):
        element = ET.SubElement(parent, f"{{{ns}}}{name}")
        element.text = value
        return element

    trigger = add(add(task, "Triggers"), "LogonTrigger")
    repetition = add(trigger, "Repetition")
    add(repetition, "Interval", f"PT{config['poll_interval_minutes']}M")
    add(repetition, "StopAtDurationEnd", "false")
    add(trigger, "Enabled", "false")
    add(trigger, "UserId", identity)
    principal = add(add(task, "Principals"), "Principal")
    principal.set("id", "Owner")
    add(principal, "UserId", identity)
    add(principal, "LogonType", "InteractiveToken")
    add(principal, "RunLevel", "LeastPrivilege")
    settings = add(task, "Settings")
    add(settings, "MultipleInstancesPolicy", "IgnoreNew")
    add(settings, "StartWhenAvailable", "true")
    add(settings, "Enabled", "false")
    add(settings, "ExecutionTimeLimit", "PT15M")
    actions = add(task, "Actions")
    actions.set("Context", "Owner")
    action = add(actions, "Exec")
    add(action, "Command", manifest["python"])
    add(action, "Arguments", subprocess.list2cmdline([
        str(root / "app" / "watcher.py"), "--once", "--config", str(root / "config.json"),
    ]))
    add(action, "WorkingDirectory", str(root / "app"))
    path = root / "task.disabled.xml"
    task_xml = ET.tostring(task, encoding="utf-16", xml_declaration=True)
    path.write_bytes(task_xml)
    return {"prepared": str(path), "task_registered": False, "task_enabled": False}


def uninstall(root):
    root = checked_root(root)
    # Suppression des seuls fichiers connus, jamais de suppression récursive.
    # L'utilisateur doit désactiver/supprimer une éventuelle tâche importée.
    for name in FILES:
        target = root / "app" / name
        if target.is_symlink() or target.is_junction():
            raise watcher.WatcherError("Fichier lié refusé.")
        target.unlink(missing_ok=True)
    for name in ("installation.json", "task.disabled.xml"):
        (root / name).unlink(missing_ok=True)
    return {"app_removed": True, "config_state_logs_preserved": True,
            "task_removed": False, "instruction": "Supprimer manuellement toute tâche préalablement importée."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "check", "prepare-task", "uninstall"])
    parser.add_argument("--root", type=Path, default=watcher.default_data_dir())
    args = parser.parse_args(argv)
    try:
        result = {"install": install, "check": diagnose,
                  "prepare-task": prepare_task, "uninstall": uninstall}[args.action](args.root)
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return 2 if result.get("ok") is False else 0
    except (watcher.WatcherError, OSError) as exc:
        print(f"Entretien local impossible : {type(exc).__name__}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
