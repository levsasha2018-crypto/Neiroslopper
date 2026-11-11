"""Read-only observer for our temporary A3 test. No serial writes or flashing.

Opening USB can reset Uno: both 12 V supplies must be OFF before using it.
The user's observation selects polarity; an LED is not a pen position sensor.
"""
from pathlib import Path
import queue
import re
import threading
import tkinter as tk
from tkinter import ttk,messagebox
from pen_lift import PenLiftSettings,load_settings,save_settings

TEST_BANNER='TEMPORARY_TEST_NOT_GRBL A3 interval_ms=2000 motor_enable=DISABLED'


def test_level(line):
    match=re.fullmatch(r'RELAY_A3_TEST ms=\d+ transitions=\d+ A3=(HIGH|LOW)',line)
    return match[1] if match else None


class RelayPolarityWindow:
    def __init__(self,parent,*,data_dir=None,on_choice=None):
        self.root=tk.Toplevel(parent);self.root.title('Проверить HIGH / LOW — без питания 12 В')
        self.root.geometry('650x460');self.root.minsize(600,430)
        self.data_dir=data_dir;self.on_choice=on_choice;self.closed=False
        self.closing=False;self.timer=None;self.close_timer=None
        self.stop=threading.Event();self.events=queue.Queue();self.thread=None;self.verified_test=False
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        frame=ttk.Frame(self.root,padding=16);frame.pack(fill='both',expand=True)
        ttk.Label(frame,text='Узнать полярность реле',font=('Segoe UI',16,'bold')).pack(anchor='w')
        ttk.Label(frame,text='Отключите ОБА БП 12 В: моторы и соленоид. Оставьте USB и 5 В релейного модуля. '
                  'Закройте соединение пульта и монитор порта. Это окно только читает сообщения тестовой прошивки; '
                  'прошивку и команды реле оно не отправляет.',wraplength=590).pack(anchor='w',pady=8)
        from serial.tools import list_ports
        ports=[p.device for p in list_ports.comports() if p.vid==0x2341 and p.pid==0x0043]
        self.port=tk.StringVar(value='COM9' if 'COM9' in ports else ports[0] if ports else '')
        self.selector=ttk.Combobox(frame,textvariable=self.port,values=ports,state='readonly',width=18)
        self.selector.pack(anchor='w')
        self.power_off=tk.BooleanVar(value=False)
        ttk.Checkbutton(frame,text='Оба источника 12 В отключены; Arduino осталась на USB',variable=self.power_off).pack(anchor='w',pady=6)
        self.start=ttk.Button(frame,text='Начать наблюдение',command=self.observe);self.start.pack(anchor='w')
        self.level=tk.StringVar(value='Уровень A3: —')
        ttk.Label(frame,textvariable=self.level,font=('Segoe UI',20,'bold')).pack(anchor='w',pady=12)
        self.status=tk.StringVar(value='Ничего не подключено. Открытие USB может перезапустить Arduino.')
        ttk.Label(frame,textvariable=self.status,wraplength=590).pack(anchor='w')
        ttk.Label(frame,text='Сравните HIGH/LOW с включением реле: по индикатору и щелчку, лучше — '
                  'по замыканию COM–NO мультиметром при отключённом 12 В. Выберите именно уровень ВКЛЮЧЕНИЯ. '
                  'Это ещё не проверка подъёма ручки.',wraplength=590).pack(anchor='w',pady=8)
        row=ttk.Frame(frame);row.pack(fill='x')
        self.choices=[]
        for value in ('HIGH','LOW'):
            b=ttk.Button(row,text=f'Реле включается на {value}',command=lambda v=value:self.choose(v),state='disabled')
            b.pack(side='left',fill='x',expand=True,padx=2);self.choices.append(b)
        self.timer=self.root.after(60,self.pump)

    def observe(self):
        if self.closed or self.closing or self.stop.is_set():return
        if self.thread and self.thread.is_alive():return
        if not self.power_off.get() or not self.port.get():
            messagebox.showerror('Сначала отключите 12 В','Нужна Arduino Uno и подтверждение отключения обоих БП 12 В.',parent=self.root);return
        if not messagebox.askokcancel('Перезапуск Arduino по USB',
                'Оба БП 12 В точно выключены? Порт будет открыт только для чтения тестовых сообщений. '
                'Проверка не подходит, если уже установлена GRBL.',parent=self.root):return
        port=self.port.get();self.start.configure(state='disabled');self.selector.configure(state='disabled')
        self.verified_test=False;self.level.set('Уровень A3: —')
        for b in self.choices:b.configure(state='disabled')
        self.status.set('Ожидаю подтверждение нашей временной тестовой прошивки…')
        def read_test():
            import serial
            import time
            try:
                with serial.Serial(port,115200,timeout=.05,write_timeout=.5) as wire:
                    known=False;deadline=time.monotonic()+8;buffer=b''
                    while not self.stop.is_set():
                        buffer+=wire.read(max(1,min(wire.in_waiting,512)))
                        if len(buffer)>2048:raise ValueError('Некорректные сообщения платы.')
                        while b'\n' in buffer:
                            raw,buffer=buffer.split(b'\n',1);line=raw.decode('ascii',errors='replace').strip()
                            if line==TEST_BANNER:known=True;self.events.put(('known',None))
                            if line.startswith('Grbl'):raise ValueError('На плате уже GRBL. Наблюдение временного теста недоступно.')
                            level=test_level(line)
                            if known and level:self.events.put(('level',level));deadline=time.monotonic()+6
                        if time.monotonic()>deadline:raise TimeoutError('Нет свежих сообщений теста A3. Порт закрыт; никаких команд не отправлено.')
            except Exception as exc:self.events.put(('error',str(exc)))
            finally:self.events.put(('end',None))
        self.thread=threading.Thread(target=read_test,daemon=True);self.thread.start()

    def choose(self,value):
        if self.closed or self.closing or not self.verified_test or value not in ('HIGH','LOW'):return
        if not messagebox.askokcancel('Сохранить наблюдение?',
                f'Вы подтвердили: реле включается на {value}. Настройка сохранится, но движение останется '
                'заблокированным до прошивки соответствующей GRBL и отдельной проверки возврата ручки. Продолжить?',parent=self.root):return
        try:
            old=load_settings(self.data_dir)
            chosen=PenLiftSettings(True,value=='LOW',False,old.up_ms,old.down_ms)
            save_settings(self.data_dir,chosen)
            if self.on_choice:self.on_choice(chosen)
            self.status.set('Полярность сохранена. Не включайте 12 В до записи подходящей GRBL.')
            self.close()
        except (OSError,ValueError) as exc:messagebox.showerror('Не сохранено',str(exc),parent=self.root)

    def pump(self):
        if self.closed or self.closing:return
        while not self.events.empty():
            kind,value=self.events.get_nowait()
            if kind=='known':
                self.verified_test=True
                for b in self.choices:b.configure(state='normal')
                self.status.set('Тестовая прошивка подтверждена. Уровень меняется каждые 2 секунды; сравните с реле.')
            elif kind=='level':self.level.set('Уровень A3: '+value)
            elif kind in ('error','end'):
                self.verified_test=False
                for b in self.choices:b.configure(state='disabled')
                if kind=='error':self.status.set(value)
                else:
                    self.start.configure(state='normal');self.selector.configure(state='readonly')
        self.timer=self.root.after(60,self.pump)

    def close(self):
        if self.closed:return
        self.closing=True;self.verified_test=False
        self.stop.set()
        for b in self.choices:b.configure(state='disabled')
        for name in ('timer','close_timer'):
            identifier=getattr(self,name)
            if identifier is not None:
                self.root.after_cancel(identifier);setattr(self,name,None)
        if self.thread and self.thread.is_alive():
            self.close_timer=self.root.after(60,self.close);return
        self.closed=True;self.root.destroy()


if __name__=='__main__':
    host=tk.Tk();host.withdraw()
    window=RelayPolarityWindow(host,data_dir=Path(__file__).parent/'data')
    def finish():
        if window.closed:host.destroy()
        else:host.after(100,finish)
    host.after(100,finish);host.mainloop()
