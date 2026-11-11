"""Scrollable secondary controls; the assistant's composer stays outside them."""
import tkinter as tk
from tkinter import ttk


class ScrollablePage(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(self, highlightthickness=0, width=300, height=180,
                                background='#eef2f6')
        self.canvas.grid(row=0, column=0, sticky='nsew')
        self.scrollbar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky='ns')
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.content = ttk.Frame(self.canvas, padding=12)
        self.window = self.canvas.create_window(0, 0, window=self.content, anchor='nw')
        self.content.bind('<Configure>', self._content_size)
        self.canvas.bind('<Configure>', self._viewport_size)

    def _content_size(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox('all'))

    def _viewport_size(self, event):
        self.canvas.itemconfigure(self.window, width=event.width)

    def enable_wheel(self):
        """Bind only this page's controls, never other windows or text editors."""
        def attach(widget):
            if widget.winfo_class() not in ('Text', 'Entry', 'TEntry', 'TCombobox', 'TScrollbar'):
                widget.bind('<MouseWheel>', self._wheel, add='+')
            for child in widget.winfo_children():
                attach(child)
        attach(self)

    def _wheel(self, event):
        if self.content.winfo_height() > self.canvas.winfo_height() and event.delta:
            self.canvas.yview_scroll(-1 if event.delta > 0 else 1, 'units')
            return 'break'
