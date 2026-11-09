"""Talent builds of hired bots: the premade spec name to ask for, and the marker talent that proves it.

A hired bot keeps the random talent build it was created with (arcane, fire or frost for a mage, 1/3 each):
`.bot role <bot> <spec>` rebuilds the STRATEGIES only (TortoiseBots commands/BotCommands.cpp). The bench therefore
whispers `talents <spec>` to the bot (TB ChangeTalentsAction: a premade spec by name) and reads the build back.

The read-back is the bot's own reply to a bare `talents` whisper: `My current talent spec is: ... Link: <link>`.
The link is one digit string per talent tree, joined by `-`, in the order of the tree's talents sorted by row then
column (TB Talentspec.cpp GetTalentLink / SortTalents); trailing zeros and empty trees are left out. A marker is
one talent of the spec's own tree: `tab` (0-2, the tree's page), `index` (its place in that tree's sorted list) and
`ranks` (its rank spells, in rank order; the first is rank 1).

Where the table comes from: Talent.dbc and TalentTab.dbc of the server's client data (row, column and rank spell
ids of every talent; the tab page is the TalentTab order field) and TB's premade links
(ai/playerbot/aiplayerbot.conf.dist.in, `AiPlayerbot.PremadeSpecLink.<class id>.<spec no>.<level>`). A marker is a
talent the spec's level-35 premade link already has at rank 1 or more and no other premade of the class has at level
60 (so it tells the spec apart), taking the deepest such row; mage fire is chosen by hand: Fire Vulnerability
(Improved Scorch), because the fire mage's Scorch rows fire only for a bot that knows it (TB MageTriggers.cpp).
`premade35` is that tree's string in the level-35 premade, kept so a test can prove the index and the premade agree.
"""

import re
from collections import namedtuple

Marker = namedtuple("Marker", "name tab index ranks premade35")

# bench spec id (trials' bot.spec, spec ids of `.bot role`) -> TB premade spec name (PremadeSpecName.<class>.<n>)
PREMADE_NAME = {
    "bm": "beastmastery", "mm": "marksmanship", "assa": "assassination", "ele": "elemental",
    "enh": "enhancement", "boomkin": "balance", "cat": "feral", "bear": "feral",
}

# (class, premade name) -> Marker
MARKERS = {
    ('warrior', 'fury'): Marker('Flurry', 1, 14, (12319, 12971, 12972, 12973, 12974), '055541200001201'),
    ('warrior', 'protection'): Marker('Gag Order', 2, 9, (12311, 12958), '253051023203'),
    ('warrior', 'arms'): Marker('Two-Handed Weapon Specialization', 0, 9, (12163, 12711, 12712), '0522531233'),
    ('paladin', 'holy'): Marker('Holy Shock', 0, 13, (20473,), '05320302035201'),
    ('paladin', 'protection'): Marker('Righteous Defense', 1, 11, (51328, 51329, 51330), '053123133203'),
    ('paladin', 'retribution'): Marker('Vengeance', 2, 12, (20049, 20056, 20057, 20058, 20059), '05203051230041'),
    ('hunter', 'beastmastery'): Marker('Bestial Discipline', 0, 9, (19590, 19592), '550032015221'),
    ('hunter', 'marksmanship'): Marker('Endless Quiver', 1, 7, (51517, 51518), '055521323'),
    ('hunter', 'survival'): Marker('Stinging Nettle', 2, 11, (51579, 51580), '350323025111'),
    ('rogue', 'combat'): Marker('Blade Rush', 1, 15, (52507, 52508), '5003502000501221'),
    ('rogue', 'assassination'): Marker('Vile Poisons', 0, 10, (16513, 16514, 16515), '005323105232'),
    ('rogue', 'subtlety'): Marker('Cloaked in Shadows', 2, 10, (52707, 52709), '503303331122'),
    ('priest', 'holy'): Marker('Book of Prayer', 1, 12, (52943, 52944), '3205020302351'),
    ('priest', 'shadow'): Marker('Darkness', 2, 15, (15259, 15307, 15308, 15309, 15310), '0522500010250121'),
    ('priest', 'discipline'): Marker('Improved Inner Fire', 0, 7, (14747, 14770), '23055022133'),
    ('shaman', 'restoration'): Marker('Restorative Totems', 2, 9, (16187, 16205, 16206, 16207, 16208), '55030321052'),
    ('shaman', 'enhancement'): Marker('Enhancing Totems', 1, 11, (16259, 16295), '5005303105022'),
    ('shaman', 'elemental'): Marker('Reverberation', 0, 6, (16040, 16113, 16114), '55133135'),
    ('mage', 'arcane'): Marker('Arcane Instability', 0, 13, (15058, 15059, 15060), '2050050310230311'),
    ('mage', 'fire'): Marker('Fire Vulnerability', 1, 9, (11095, 12872, 12873), '50523201030302'),
    ('mage', 'frost'): Marker('Arctic Reach', 2, 10, (16757, 16758), '0533020510232'),
    ('warlock', 'affliction'): Marker('Shadow Mastery', 0, 16, (18271, 18272, 18273, 18274, 18275), '05032032022321001'),
    ('warlock', 'demonology'): Marker('Power Overwhelming', 1, 13, (51714,), '05003315002031'),
    ('warlock', 'destruction'): Marker('Improved Immolate', 2, 12, (17815, 17833, 17834, 17835, 17836), '55050015020021'),
    ('druid', 'balance'): Marker('Owlkin Frenzy', 0, 13, (45734, 51426, 51427), '500002303125311'),
    ('druid', 'feral'): Marker('Predatory Strikes', 1, 10, (16972, 16974, 16975), '55303021322'),
    ('druid', 'restoration'): Marker('Gift of Nature', 2, 8, (17104, 24943, 24944, 24945, 24946), '5050013354'),
}

LINK_REPLY = re.compile(r"talent spec is:.*?Link:\s*(\S*)", re.I | re.S)
LINK_SHAPE = re.compile(r"[0-5]*(?:-[0-5]*){0,2}")      # a rank is 0-5; up to three trees joined by `-`
SPEC_SHARE_MIN = 0.6        # the spec's own tree holds at least this share of all spent points


def premade_name(spec):
    spec = str(spec or "").strip().lower()
    return PREMADE_NAME.get(spec, spec)


def marker_for(klass, spec):
    """The Marker for a class and bench spec, or None (no marker: the build cannot be proven)."""
    return MARKERS.get((str(klass or "").strip().lower(), premade_name(spec)))


def link_text(text):
    """The raw string after `Link:` in a `talents` reply (clean text), or None when the text has none."""
    m = LINK_REPLY.search(text or "")
    return m.group(1) if m else None


def parse_link(text):
    """The three tree strings of a `talents` reply (clean text), or None when it carries no well-formed link
    (no `Link:`, an empty one, a rank above 5, more than three trees)."""
    raw = link_text(text)
    if not raw or not LINK_SHAPE.fullmatch(raw):
        return None
    parts = raw.split("-")
    return [parts[i] if i < len(parts) else "" for i in range(3)]


def marker_rank(trees, marker):
    """The marker talent's rank in the parsed link (0 when its place is past the end of the tree string)."""
    tree = trees[marker.tab]
    return int(tree[marker.index]) if marker.index < len(tree) else 0


def tree_points(trees):
    """Points spent per tree of a parsed link."""
    return [sum(int(ch) for ch in tree) for tree in trees]


def spec_share(trees, marker):
    """(points in the spec's own tree, all points, share 0-1): a stale hybrid with the marker talent but most of
    its points elsewhere is not the spec."""
    points = tree_points(trees)
    total = sum(points)
    return points[marker.tab], total, (points[marker.tab] / total if total else 0.0)
