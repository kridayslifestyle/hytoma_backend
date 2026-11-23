
"""
HYTOMA customer advisory-lock integration test.

Requires an accessible PostgreSQL DATABASE_URL.
Does not send WhatsApp messages.
Does not insert, update, or delete customer data.
"""

import threading
import time

from db import customer_send_guard


TEST_PHONE = "hytoma_lock_test_001"

first_has_lock = threading.Event()
release_first = threading.Event()
second_has_lock = threading.Event()

errors = []


def first_operation():
    try:
        with customer_send_guard(TEST_PHONE):
            first_has_lock.set()

            # Keep the lock until the test releases it.
            if not release_first.wait(timeout=10):
                raise TimeoutError(
                    "First operation release timed out"
                )

    except Exception as exc:
        errors.append(("first", str(exc)))


def second_operation():
    try:
        with customer_send_guard(TEST_PHONE):
            second_has_lock.set()

    except Exception as exc:
        errors.append(("second", str(exc)))


def run_test():
    first = threading.Thread(
        target=first_operation,
        daemon=True,
    )

    second = threading.Thread(
        target=second_operation,
        daemon=True,
    )

    try:
        first.start()

        assert first_has_lock.wait(timeout=5), (
            "First operation could not acquire lock"
        )

        second.start()

        # Second operation should be blocked because
        # the first still holds the customer lock.
        time.sleep(0.5)

        assert not second_has_lock.is_set(), (
            "FAILED: Both operations acquired "
            "the same customer lock simultaneously"
        )

        print(
            "PASS: Second operation blocked "
            "while first holds the lock"
        )

    finally:
        release_first.set()

        first.join(timeout=10)
        second.join(timeout=10)

    assert not first.is_alive(), (
        "First operation did not finish"
    )

    assert not second.is_alive(), (
        "Second operation did not finish"
    )

    assert not errors, f"Worker errors: {errors}"

    assert second_has_lock.is_set(), (
        "FAILED: Second operation could not "
        "acquire lock after release"
    )

    print(
        "PASS: Second operation acquired "
        "the lock after first released it"
    )

    print(
        "SUCCESS: PostgreSQL customer lock "
        "coordination test passed"
    )


if __name__ == "__main__":
    run_test()
