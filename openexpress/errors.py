"""What the errors that are a message for the user have in common."""


def sentence(error) -> str:
    """The text of an error as a sentence of its own: with a full stop when it ends without
    one, before the next sentence of a message ("…did not answer in time. Unplug…")."""
    text = str(error).rstrip()
    return text if text.endswith((".", "!", "?", "…")) else text + "."


class UserFacingError(Exception):
    """An error whose words are meant for the user: they say what happened or what to do,
    and the program shows them as they are (the device not ready yet, not on the account,
    an answer of a server, a refusal). The window's log writes it in one line, without the
    traceback kept for an error nobody expected."""


class OrdinaryStop(UserFacingError):
    """Not an error: the program stopped as it should, waiting for the user or for something
    else to end (the choice of the first sync, another sync running, a notice about the USB
    mode of the device…). The user is shown its words; the log says it in one line of INFO,
    `log_reason`, never as a failure."""

    log_reason = "stopped as it should"
