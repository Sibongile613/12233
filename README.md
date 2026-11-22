# Lone Wolf Scratch Bot

A new Python bot for the Builders Mech Royale track. Its decision logic is written for this project; mechfight.py is the protocol wrapper for JSON-line input/output and match lifecycle.

## Build

From this folder, build the Docker image with:

    docker build -t lone-wolf-scratch .

The official scorer starts the image with the map mounted at /arena, one CPU, 512 MB of memory, no network, and an unprivileged user. The bot reads the map path from the hello message and keeps its runtime state in memory.

## Strategy

- Stay inside the safe zone and use its outer ring as a fallback route.
- Fight when a visible enemy is weakened, appears to be facing away, or is near the zone boundary.
- Avoid a nearby enemy when none of those openings is present.
- Aim using target velocity and solve a low ballistic arc for the mortar.
- Fire only when the weapon is in range and actual aim is close to requested aim.
- Seek a useful second weapon and nearby cubes only when the nearest mech is far enough away.
- Sidestep a visible incoming projectile only when the dodge remains in the current safe area.

This is a fresh, unscored candidate. Its score is unknown until it passes the official check and completes practice matches.
