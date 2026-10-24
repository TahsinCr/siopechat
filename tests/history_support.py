"""Small reusable history records for archive and paging tests."""


def message(index, size=400):
    return {"id": str(index), "role": "assistant", "text": f"Answer {index}: " + "x" * size}
