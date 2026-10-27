"""`python worker.py` prints the registered jobs at boot; that must not crash on arq Function wrappers."""

import worker


def test_function_names_handles_arq_function_wrappers():
    names = worker.function_names()
    assert names and all(isinstance(n, str) and n for n in names)
    # Registered through arq's func(...) wrapper (has .name, no __name__): the case that crashed the worker.
    assert "process_whatsapp_event" in names
    assert "send_whatsapp_message" in names
