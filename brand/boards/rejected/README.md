# Rejected boards — do not ship

These are the first-generation boards. Each one contains **the image generator's own
attempt at the mark**: rounded chevrons, an off-centre dot, and an outlined chevron
that comes back filled at small sizes. They were produced by prompts that *described*
the mark, which is the prompt shape that reliably produces a near-miss.

They are kept only as evidence of that failure mode. Nothing here is canonical, no
spec points at them, and the compositor's `--check` refuses a spec whose base is not a
`-raw` file. The shipped boards are the `*-composed.png` files one directory up, and
each of those is recorded in `../COMPOSITES.json` against the locked mark geometry.
