"""Keep basic-stack workers schedulable in interactive mode."""

from sidera_control_plane.basic_acceptance_heartbeat import main

if __name__ == "__main__":
    raise SystemExit(main())
