class System:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def to_dict(self):
        return {"id": self.id, "name": self.name}

class Node:
    def __init__(self, id, name, system_id):
        self.id = id
        self.name = name
        self.system_id = system_id

    def to_dict(self):
        return {"id": self.id, "name": self.name, "system_id": self.system_id}

class Volume:
    def __init__(self, id, name, system_id):
        self.id = id
        self.name = name
        self.system_id = system_id

    def to_dict(self):
        return {"id": self.id, "name": self.name, "system_id": self.system_id}

class Host:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    def to_dict(self):
        return {"id": self.id, "name": self.name}

class Settings:
    def __init__(self, id, system_id, snapshot_frequency=None):
        self.id = id
        self.system_id = system_id
        self.snapshot_frequency = snapshot_frequency  # Frequency in minutes/hours/days

    def to_dict(self):
        return {
            "id": self.id,
            "system_id": self.system_id,
            "snapshot_frequency": self.snapshot_frequency
        }
