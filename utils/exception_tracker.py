"""
Centralised exception logger.

Usage inside any except block:

    from utils.exception_tracker import log_exception

    except Exception as e:
        log_exception(e)            # records to exception_log table
        # ... existing handling ...
"""

import sys
import traceback as tb_module
from datetime import datetime

from flask import has_request_context
from flask_login import current_user

from database import db
from database.models import ExceptionLog


def log_exception(exc: Exception | None = None) -> None:
    """Record an exception to the exception_log table.

    Safe to call from anywhere — inside or outside a request context.
    Never raises; if the recording itself fails it is silently swallowed
    so the original error handling is not disrupted.
    """
    try:
        if exc is None:
            exc_type_obj, exc_val, exc_tb = sys.exc_info()
        else:
            exc_type_obj = type(exc)
            exc_val = exc
            exc_tb = exc.__traceback__

        exc_type_name = exc_type_obj.__name__ if exc_type_obj else 'Unknown'
        exc_message = str(exc_val) if exc_val else ''
        tb_text = ''.join(tb_module.format_exception(exc_type_obj, exc_val, exc_tb)) if exc_tb else None

        # Extract source location from the deepest traceback frame
        module_name = None
        func_name = None
        lineno = None
        fname = None
        if exc_tb:
            frame = exc_tb
            while frame.tb_next:
                frame = frame.tb_next
            lineno = frame.tb_lineno
            func_name = frame.tb_frame.f_code.co_name
            fname = frame.tb_frame.f_code.co_filename
            module_name = frame.tb_frame.f_globals.get('__name__', '')

        # Logged-in user email (if inside a request)
        user_email = None
        if has_request_context():
            try:
                if current_user and current_user.is_authenticated:
                    user_email = current_user.id  # id is the email string
            except Exception:
                pass

        entry = ExceptionLog(
            exception_type=exc_type_name,
            exception_message=exc_message[:65535] if exc_message else '',
            traceback=tb_text,
            module=module_name,
            function=func_name,
            line_number=lineno,
            filename=fname,
            user_email=user_email,
            timestamp=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        db.session.rollback()
