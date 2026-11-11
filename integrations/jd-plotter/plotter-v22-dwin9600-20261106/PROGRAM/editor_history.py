"""Bounded, copied editor history. Never stores serial state or machine origins."""
from copy import deepcopy


class EditHistory:
    def __init__(self, limit=30):
        self.limit = limit
        self.past = []
        self.future = []

    def clear(self):
        self.past.clear()
        self.future.clear()

    def push(self, state):
        self.past.append(deepcopy(state))
        del self.past[:-self.limit]
        self.future.clear()

    def undo(self, current):
        if not self.past:
            raise ValueError("Нет изменений для отмены.")
        self.future.append(deepcopy(current))
        return self.past.pop()

    def redo(self, current):
        if not self.future:
            raise ValueError("Нет изменений для повтора.")
        self.past.append(deepcopy(current))
        del self.past[:-self.limit]
        return self.future.pop()
