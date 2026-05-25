from __future__ import annotations


def main() -> None:
    import sounddevice as sd

    print(sd.query_devices())


if __name__ == "__main__":
    main()
