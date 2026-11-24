"""The help of the commands: the folders they use by default as the program finds them (in the
Documents folder of the user's XDG folders, with ~ for the home folder), the folders of
`activities` and `activities export` as DIR, the file of `fit inspect` in capitals (every name:
test_cli_argument_names); each command's own page says what it does, as the list of commands
does, and the list is not repeated in the usage line."""

import argparse
from pathlib import Path

import pytest

from openexpress import cli, paths


def help_of(capsys, *argv) -> str:
    with pytest.raises(SystemExit) as stopped:
        cli.main([*argv, "--help"])
    assert stopped.value.code == 0
    return capsys.readouterr().out


@pytest.fixture
def dokumenty(monkeypatch):
    """The backups where the program finds them on a Polish desktop: in Garmin in its XDG
    Documents folder, ~/Dokumenty."""
    monkeypatch.setattr(paths, "default_backup_dir",
                        lambda: Path.home() / "Dokumenty" / "Garmin" / "Backups")


def test_the_default_folders_are_said_as_the_program_finds_them(capsys, dokumenty):
    for argv in (("backup",), ("activities",)):
        said = help_of(capsys, *argv)
        assert "~/Dokumenty/Garmin/Backups" in said and "Documents" not in said
    export = help_of(capsys, "activities", "export")
    assert "~/Dokumenty/Garmin/GPX" in export and "~/Dokumenty/Garmin/Backups" in export
    assert "Documents" not in export


def test_a_folder_outside_the_home_folder_is_said_whole(capsys, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "default_backup_dir", lambda: Path("/srv/garmin/Backups"))
    assert "(default: /srv/garmin/Backups)" in help_of(capsys, "backup")


def test_the_backups_of_an_export_and_of_the_list_are_a_dir(capsys):
    export = help_of(capsys, "activities", "export")
    assert "EXPORT_BACKUPS" not in export and "--backups DIR" in export
    assert "--backups DIR" in help_of(capsys, "activities")


def test_the_file_of_fit_inspect_in_capitals(capsys):
    said = help_of(capsys, "fit", "inspect")
    assert "usage: openexpress fit inspect [-h] FILE" in said
    assert "\n  FILE " in said and " file\n" not in said


def test_a_path_is_shown_with_a_tilde_for_the_home_folder():
    assert paths.shown_path(Path.home() / "Dokumenty" / "Garmin") == "~/Dokumenty/Garmin"
    assert paths.shown_path(Path("/srv/garmin")) == "/srv/garmin"


def _commands(parser, words=()):
    """Each command and subcommand: its words, its parser and its line in the list of commands."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            lines = {choice.dest: choice.help for choice in action._choices_actions}
            for name, command in action.choices.items():
                yield (*words, name), command, lines.get(name)
                yield from _commands(command, (*words, name))


def test_each_command_says_on_its_own_page_what_it_does(capsys):
    commands = list(_commands(cli.build_parser()))
    assert len(commands) > 40
    for words, parser, line in commands:
        assert line and parser.description == line, " ".join(words)
    assert "sync with Garmin Connect (dry run by default)" in help_of(capsys, "sync")


def help_in(language, capsys, *argv) -> str:
    """A page of the help in `language`, its words apart from how it is wrapped."""
    from openexpress import i18n

    try:
        return " ".join(help_of(capsys, "--lang", language, *argv).split())
    finally:
        i18n.setup("en")


@pytest.mark.parametrize("language, said", [
    ("en", ("also ask Garmin Connect and Garmin's update server, or use their answers from a "
            "moment ago")),
    ("pl", ("zapytaj też Garmin Connect i serwer aktualizacji Garmina albo użyj ich odpowiedzi "
            "sprzed chwili"))])
def test_status_online_says_it_may_use_answers_from_a_moment_ago(capsys, language, said):
    assert said in help_in(language, capsys, "status")


def test_free_space_is_said_as_the_button_of_the_window(capsys):
    assert ("--free-space free up space on the device: older memory dumps"
            in help_in("en", capsys, "diagnostics"))


@pytest.mark.parametrize("language, command, said, left_out", [
    ("en", "baseline", "--apply mark the files", "mark the files (nothing"),
    ("pl", "baseline", "--apply oznacz pliki", "oznacz pliki (nic"),
    ("en", "sync", "--apply upload and write", "(without it"),
    ("pl", "sync", "--apply wyślij i zapisz", "(bez tego"),
])
def test_apply_does_not_repeat_what_the_command_says_above_it(capsys, language, command, said,
                                                              left_out):
    page = help_in(language, capsys, command)
    assert said in page and left_out not in page


@pytest.mark.parametrize("language, said, left_out", [
    ("en", "the languages, the copy of the device's diagnostic files", "diagnostics copy"),
    ("pl", "języki, kopia plików diagnostycznych urządzenia", "kopia diagnostyki"),
])
def test_setup_names_the_copy_as_the_program_does(capsys, language, said, left_out):
    for page in (help_in(language, capsys), help_in(language, capsys, "setup")):
        assert said in page and left_out not in page


@pytest.mark.parametrize("language, said", [
    ("en", "with --type, or -v before the command: only the last N files"),
    ("pl", "z --type albo z -v przed poleceniem: tylko ostatnie N plików")])
def test_the_limit_of_files_says_where_v_goes(capsys, language, said):
    assert said in help_in(language, capsys, "files")


def test_the_usage_line_does_not_list_every_command(capsys):
    said = help_of(capsys)
    usage = " ".join(said.split("\n\n")[0].split())  # the usage line, however it is wrapped
    assert usage.startswith("usage: openexpress ") and "COMMAND" in usage
    assert "{setup," not in said
