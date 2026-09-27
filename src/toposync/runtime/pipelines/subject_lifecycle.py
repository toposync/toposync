"""Sequências terminais compactas; identidades legadas preservam abstenção no limite."""

from bisect import bisect_left


class ClosedSubjectLedger:
    def __init__(self, capacity=4096, *, source_index=0):
        self.source_index = source_index
        self.capacity = capacity
        self.capacity_reached = False
        self._legacy = set()
        self._intervals = {}
        self._active = {}
        self._sources = set()

    def __len__(self):
        return len(self._legacy) + sum(map(len, self._intervals.values()))

    def __iter__(self):
        return iter(self._legacy)

    def __contains__(self, key):
        return key in self._legacy

    def clear(self):
        self._legacy.clear()
        self._intervals.clear()
        self._active.clear()
        self._sources.clear()
        self.capacity_reached = False

    def observe(self, key, subject, *, retain=True):
        """Confere encerramento; retém descritores somente quando admitidos.

        Época/sequência pertencem ao produtor. Intervalos disjuntos preservam
        aberturas atrasadas e nunca usam o tempo de captura como identidade.
        """
        epoch = subject.get("lifecycle_epoch")
        sequence = subject.get("lifecycle_sequence")
        if epoch is None and sequence is None:
            return key in self._legacy or key[:2] in self._sources
        if (
            not all(isinstance(part, str) for part in key)
            or not key[self.source_index] or not key[2]
            or not isinstance(epoch, str) or not epoch or len(epoch) > 128
            or type(sequence) is not int or not 0 < sequence < 2**63
        ):
            return True
        scope = (*key[:2], epoch)
        ranges = self._intervals.get(scope, [])
        index = bisect_left(ranges, (sequence + 1,))
        if index and ranges[index - 1][1] >= sequence:
            return True
        descriptor = (scope, sequence)
        previous = self._active.get(key)
        if previous is not None and previous != descriptor:
            # Identidade ativa não troca de encarnação sem encerramento.
            return True
        if not retain:
            return False
        if key[:2] not in self._sources and len(self._sources) >= self.capacity:
            self.capacity_reached = True
            return True
        self._sources.add(key[:2])
        if previous is None and len(self._active) >= self.capacity:
            self.capacity_reached = True
            return True
        self._active[key] = descriptor
        return False

    def retain_active(self, keys):
        retained = set(keys)
        self._active = {key: value for key, value in self._active.items() if key in retained}

    def forget_active(self, key):
        self._active.pop(key, None)

    def close(self, key, subject=None):
        if subject is not None and self.observe(key, subject):
            return
        self.add(key)

    def add(self, key):
        descriptor = self._active.pop(key, None)
        if descriptor is None:
            if key in self._legacy:
                return
            if len(self) >= self.capacity:
                self.capacity_reached = True
                return
            self._legacy.add(key)
            return
        scope, sequence = descriptor
        ranges = self._intervals.get(scope, [])
        index = bisect_left(ranges, (sequence,))
        start = end = sequence
        if index and ranges[index - 1][1] >= sequence - 1:
            index -= 1
            start, end = ranges[index]
            end = max(end, sequence)
        stop = index
        while stop < len(ranges) and ranges[stop][0] <= end + 1:
            end = max(end, ranges[stop][1])
            stop += 1
        if len(self) + 1 - (stop - index) > self.capacity:
            self.capacity_reached = True
            return
        ranges[index:stop] = [(start, end)]
        self._intervals[scope] = ranges
