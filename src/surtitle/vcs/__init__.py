"""Version control: the tools, what they say about a project, and how to use them.

Three concerns, kept apart because they fail differently:

* :mod:`surtitle.vcs.provision` gets portable ``git`` and ``svn`` onto a machine
  that has neither, without an installer or an administrator;
* :mod:`surtitle.vcs.repo` reads what a working copy says, and changes nothing;
* :mod:`surtitle.vcs.commit` makes the commit — the one mutating step, and the one
  that only ever happens after the user has been asked;
* :mod:`surtitle.vcs.guide` is the text the agent reads before doing any of it.
"""

from __future__ import annotations

from surtitle.vcs import commit, guide, provision, repo

__all__ = ["commit", "guide", "provision", "repo"]
