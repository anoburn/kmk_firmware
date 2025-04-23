try:
    from typing import Optional, Tuple, Union
except ImportError:
    pass
from micropython import const

from kmk.keys import Key, make_key
from kmk.kmk_keyboard import KMKKeyboard
from kmk.modules import Module
from kmk.utils import Debug

from supervisor import ticks_ms
debug = Debug(__name__)


class _ComboState:
    RESET = const(0)
    MATCHING = const(1)
    ACTIVE = const(2)
    IDLE = const(3)


class Combo:
    fast_reset = False
    per_key_timeout = False
    timeout = 50
    _remaining = []
    _timeout = None
    _match_coord = False

    def __init__(
        self,
        match: Tuple[Union[Key, int], ...],
        result: Key,
        fast_reset=None,
        per_key_timeout=None,
        timeout=None,
        match_coord=None,
        pressed=[]
    ):
        '''
        match: tuple of keys (KC.A, KC.B)
        result: key KC.C
        '''
        self.match = match
        self.result = result
        if fast_reset is not None:
            self.fast_reset = fast_reset
        if per_key_timeout is not None:
            self.per_key_timeout = per_key_timeout
        if timeout is not None:
            self.timeout = timeout
        if match_coord is not None:
            self._match_coord = match_coord
        self._state = _ComboState.RESET

    @property
    def state(self):
        return self._state

    @state.setter
    def state(self, new_state):
        if self._state == new_state:
            return
        if new_state == _ComboState.MATCHING:
            Combos.match_count += 1
        if self._state == _ComboState.MATCHING:
            Combos.match_count -= 1
        self._state = new_state

    def __repr__(self):
        return f'{self.__class__.__name__}({list(self.match)})'

    def matches(self, key: Key, int_coord: int):
        raise NotImplementedError

    def has_match(self, key: Key, int_coord: int):
        return self._match_coord and int_coord in self.match or key in self.match

    def uses_match(self, key: Key, int_coord: int):
        return self.has_match(key, int_coord)

    def insert(self, key: Key, int_coord: int):
        if self._match_coord:
            self._remaining.insert(0, int_coord)
        else:
            self._remaining.insert(0, key)

    def unpress(self, key: Key, int_coord: int):
        if self._match_coord:
            self.pressed.remove(int_coord)
        else:
            self.pressed.remove(key)

    def reset(self):
        self._remaining = list(self.match)
        self.pressed = []
        self.state = _ComboState.MATCHING


class Chord(Combo):
    def matches(self, key: Key, int_coord: int):
        if not self._match_coord and key in self._remaining:
            self._remaining.remove(key)
            return True
        elif self._match_coord and int_coord in self._remaining:
            self._remaining.remove(int_coord)
            return True
        else:
            return False

class Sequence(Combo):
    fast_reset = True
    per_key_timeout = True
    timeout = 1000

    def matches(self, key: Key, int_coord: int):
        if (
            not self._match_coord and self._remaining and self._remaining[0] == key
        ) or (
            self._match_coord and self._remaining and self._remaining[0] == int_coord
        ):
            self.pressed.append(self._remaining.pop(0))
            return True
        else:
            return False
        
    def uses_match(self, key: Key, int_coord: int):
        if self.has_match(key, int_coord) and (
            (self._match_coord and int_coord in self.pressed) or
            (not self._match_coord and key in self.pressed)
        ):
            return True
        return False

class Combos(Module):
    match_count = 0
    start_timepoint = None
    def __init__(self, combos=[]):
        Combos.match_count = 0
        Combos.start_timepoint = None
        self.combos = combos
        self._key_buffer = []
        self._pending_combos = []
        self._timeout = None

        make_key(names=('LEADER', 'LDR'))

    def during_bootup(self, keyboard):
        self.reset_combos()

    def before_matrix_scan(self, keyboard):
        return

    def after_matrix_scan(self, keyboard):
        return

    def before_hid_send(self, keyboard):
        return

    def after_hid_send(self, keyboard):
        return

    def on_powersave_enable(self, keyboard):
        return

    def on_powersave_disable(self, keyboard):
        return

    def process_key(self, keyboard, key: Key, is_pressed, int_coord):
        if is_pressed:
            self.on_press(keyboard, key, int_coord)
        else:
            self.on_release(keyboard, key, int_coord)

    def on_press(self, keyboard: KMKKeyboard, key: Key, int_coord: Optional[int]):
        if self._timeout:
            keyboard.cancel_timeout(self._timeout)
        current_timepoint = ticks_ms()
        if self.start_timepoint is None:
            self.start_timepoint = current_timepoint
        last_timepoint = self._key_buffer[-1][-1] if self._key_buffer else self.start_timepoint

        longest_timeout = 0
        matching_unfinished = 0
        self._pending_combos = []
        d_last = (current_timepoint - last_timepoint) / 1000
        d_start = (current_timepoint - self.start_timepoint) / 1000
        for combo in self.combos:
            if combo.state != _ComboState.MATCHING:
                continue
            if (
                combo.matches(key, int_coord)
            ) and ((
                combo.per_key_timeout and d_last < combo.timeout
            ) or (
                not combo.per_key_timeout and d_start < combo.timeout
            )):
                if len(combo._remaining) == 0:
                    self._pending_combos.append(combo)
                else:
                    matching_unfinished += 1
                longest_timeout = max(longest_timeout, combo.timeout)
            else:
                combo.reset()
                combo.state = _ComboState.RESET

        if Combos.match_count == 0:
            # if buffered combos / keys left, process them first and try again
            if self._pending_combos or self._key_buffer:
                self.flush_buffers(keyboard)
                self.start_timepoint = current_timepoint
                self.process_key(keyboard, key, True, int_coord)
            else:
                keyboard.resume_process_key(self, key, True, int_coord)
                self.reset_combos()
            return
        
        self._key_buffer.append((int_coord, key, True, current_timepoint))

        if matching_unfinished == 0:
            self.send_pending_combos(keyboard)
            return
            
        self._timeout = keyboard.set_timeout(longest_timeout, lambda: self.on_timeout(keyboard))

    def on_release(self, keyboard: KMKKeyboard, key: Key, int_coord: Optional[int]):
        if self._timeout:
            keyboard.cancel_timeout(self._timeout)
        current_timepoint = ticks_ms()
        longest_timeout = 0
        propagate_release = True
        buffer_release = False

        for combo in self.combos:
            if not combo.uses_match(key, int_coord):
                continue

            if combo.state == _ComboState.ACTIVE:
                self.deactivate(keyboard, combo)
                propagate_release = False
                if combo.fast_reset:
                    combo.reset()
                else:
                    combo.state = _ComboState.MATCHING
            

            if combo.state == _ComboState.MATCHING:
                if combo.fast_reset:
                    # Sequence. Do nothing, set timeout for next required key
                    longest_timeout = max(longest_timeout, combo.timeout)
                    propagate_release = False
                    buffer_release = True
                    combo.unpress(key, int_coord)
                else:
                    combo.reset()


        # Don't propagate key-release events for keys that have been
        # buffered. Append release events only if corresponding press is in
        # buffer.
        press_vs_released = sum([
            buffer_event[2]*2 - 1 for buffer_event in self._key_buffer
            if buffer_event[0] == int_coord and buffer_event[1] == key
        ])
        if press_vs_released > 0:
            self._key_buffer.append((int_coord, key, False, current_timepoint))
            propagate_release = False

        # Reset on non-combo key up
        if Combos.match_count == 0:
            self.reset_combos()
            self._key_buffer = []
        else:
            if longest_timeout:
                self._timeout = keyboard.set_timeout(longest_timeout, lambda: self.on_timeout(keyboard))
            else:
                self.flush_buffers(keyboard)
        
        if propagate_release:
            keyboard.resume_process_key(self, key, False, int_coord)

    def send_pending_combos(self, keyboard):
        buffer_pressed_vs_released = {}
        for buffer_coord, buffer_key, buffer_pressed, _ in self._key_buffer:
            if (buffer_coord, buffer_key) not in buffer_pressed_vs_released:
                buffer_pressed_vs_released[(buffer_coord, buffer_key)] = 0
            if buffer_pressed:
                buffer_pressed_vs_released[(buffer_coord, buffer_key)] += 1
            else:
                buffer_pressed_vs_released[(buffer_coord, buffer_key)] -= 1
    
        for combo in self._pending_combos:
            self.activate(keyboard, combo)
            combo.state = _ComboState.ACTIVE
            # If a key of the combo was released in the buffer at least as many times as it was pressed, reset
            for (buffer_coord, buffer_key), count in buffer_pressed_vs_released.items():
                if count <= 0 and combo.has_match(buffer_key, buffer_coord):
                    self.deactivate(keyboard, combo)
                    combo.state = _ComboState.RESET
                    break
        self._pending_combos = []
        self._key_buffer = []
        self.reset_combos()
        self.start_timepoint = None

    def flush_buffers(self, keyboard: KMKKeyboard):
        if self._pending_combos:
            self.send_pending_combos(keyboard)

        # "else:"   (because key buffer gets emptied in send_pending_combos())
        # send the key buffer until after first key press, then try processing the rest again
        while self._key_buffer:
            int_coord, key, is_pressed, timepoint = self._key_buffer.pop(0)
            keyboard.resume_process_key(self, key, is_pressed, int_coord)
            if is_pressed:
                self.reset_combos()
                self.start_timepoint = timepoint
                old_buffer = self._key_buffer.copy()
                self._key_buffer = []
                for buffer_int_coord, buffer_key, buffer_is_pressed, _ in old_buffer:
                    if not buffer_is_pressed and key == buffer_key and int_coord == buffer_int_coord:
                        keyboard.resume_process_key(self, buffer_key, buffer_is_pressed, buffer_int_coord)
                    self.process_key(keyboard, buffer_key, buffer_is_pressed, buffer_int_coord)

    def on_timeout(self, keyboard):
        self.start_timepoint = None
        self.flush_buffers(keyboard)

    def send_key_buffer(self, keyboard):
        for int_coord, key, is_pressed, _ in self._key_buffer:
            keyboard.resume_process_key(self, key, is_pressed, int_coord)

    def activate(self, keyboard, combo):
        if debug.enabled:
            debug('activate', combo)
        keyboard.resume_process_key(self, combo.result, True)

    def deactivate(self, keyboard, combo):
        if debug.enabled:
            debug('deactivate', combo)
        keyboard.resume_process_key(self, combo.result, False)

    def reset_combos(self):
        for combo in self.combos:
            if combo.state != _ComboState.ACTIVE:
                combo.reset()
