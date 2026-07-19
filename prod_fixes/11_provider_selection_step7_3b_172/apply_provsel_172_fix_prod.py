#!/usr/bin/env python3
r"""Apply the CORRECT Provider Selection Step 7 (3B) fix to PROD — fully self-
contained, hardcoded to the 6 affected claims, deterministic, NO LLM. Verdict
stays CLEAN. No external files, no replica, no DB reads to derive values.

Why this script exists
----------------------
An earlier run of ``fix_provider_selection_confirm_denied_prod.py`` was applied to
prod while it still targeted the **3rd choice** (``step:12:7:3``). The auditor
actually flagged **UI rule #172 = the 5th choice** (``step:12:7:5``). So on prod we
must, for the 6 affected claims:

    1. REVERT the wrong 3rd-choice change  (step:12:7:3 -> original not-matched/DENY)
    2. APPLY  the correct 5th-choice fix   (step:12:7:5 -> matched/CONFIRMED, #172)

The exact corrected values (both eval rows + the Provider Selection trace entries)
are BAKED INTO this file as a compressed blob, captured from the verified local
prod-replica. Nothing is read from disk or re-derived. For each of the 6 runs the
script writes:

    * RuleEvaluation step:12:7:3  <- reverted original values
    * RuleEvaluation step:12:7:5  <- the #172 fix (matched, CONFIRMED, ALLOW)
    * ClaimTrace: the Provider Selection entries are replaced with the corrected
      ones (all OTHER SOP entries on the claim are preserved untouched);
      final_status + explainability_json are recomputed (stays CLEAN).

RuleExecutionRun verdict + ClaimExecutiveSummary are LEFT UNCHANGED (already CLEAN).

Idempotent: re-running writes the same values. Transactional per claim; if a claim
would not stay CLEAN the write is rolled back.

Usage (prod box):
    python scripts/apply_provsel_172_fix_prod.py --dry-run
    python scripts/apply_provsel_172_fix_prod.py --apply
"""
from __future__ import annotations

import argparse
import base64
import gzip
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


def _find_repo_root(start: str) -> str:
    d = start
    for _ in range(6):
        if os.path.exists(os.path.join(d, "manage.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return start


REPO_ROOT = _find_repo_root(_HERE)
PS_TITLE = "OBH Facets Provider Selection Guidelines"
EVAL_KEYS = ("step:12:7:3", "step:12:7:5")
EVAL_FIELDS = [
    "matched",
    "skipped",
    "decision_type",
    "verdict",
    "reasoning",
    "confidence",
    "codes",
]

# Hardcoded scope: the 6 affected runs (run_id -> claim_id). These run_ids are the
# same on prod and the replica (the replica is a faithful copy of prod).
RUNS = {
    "7db29d1d-e02a-4bb3-a456-bd5eafa5ae23": "25XI83367000",
    "6d0bbe60-16bc-4a98-bdcd-f7b1046ba649": "25XJ05182100",
    "f7e97e17-cd55-4b7a-bfb5-ec3c3c0c3689": "25XJ43174200",
    "b1d8ad41-8c07-4103-9c78-ae19c94ba6f0": "25XJ46669600",
    "3205e5cc-64ef-4faa-9c89-5e9bf86f6215": "25XJ91841200",
    "d22f1c74-913a-42dc-bec6-4c477bce5edf": "25XK02420100",
}

# Baked-in PROD Postgres (overridable by any PG_* env var already set).
_PROD_ENV = {
    "APP_ENV": "prod",
    "DJANGO_SETTINGS_MODULE": "sop_backend.settings",
    "LLM_BACKEND": "none",
    "NO_LLM": "1",
    "PG_HOST": "azure-pgsql-flexibleserver-np-390744103630-dev.privatelink.postgres.database.azure.com",
    "PG_PORT": "5432",
    "PG_USER": "pgazdev",
    "PG_PASSWORD": "Xudzab-doxsoz-1vudra",
    "PG_DATABASE": "uhc_backend",
}

# --- BAKED-IN CORRECTED VALUES (gzip+base64 of the per-run backup json) ----------
_BACKUP_B64GZ = (
    "H4sIACB9WGoC/+y9+XfiytUo+q/U62QFuA9hQIw+yz9gwDaJjQnQZ0hOlpdAwtZ3MOIi6G6v7+V/f3vXpCoNIAa7u306Nzdf"
    "25ZKVXueau///VC3J+WmXbINp1i2jMpkYhpWpVozJnbVsWZW1XLK5ofz//0wnVvu84Nrfzj/UK7+2muYZq1eLBY/5D84n6y5"
    "j4/4a2d5Xiqf18/pG8/WevrkwAsz+LuT/+D/4S6Xys+2M3V911s8rF+WDizb6fZ/g+U+OSvbna7hF/DDyrF8b+EuHuHH8ZPr"
    "k9Vm7pCV83837srxiTWfk/XTynHI1FvY7hpW889JtpQj1ytvs4QHp97KJrOV90wGK++Tazsr0l2s3fULGcNX8yRbzpEevAp/"
    "2lhzAl+YuPO5YxNvQTr3bbNWJPTkxH22HuF5a2GTrJkj9/d9kr3frIk3I31n/dlb/ZErkF+eXNjeYDgYPnT74974t4vMdQb3"
    "NnNXz7BdbVtssfWTIz5EP0H8J+8zPLogbrCtbPum2yfDj/e/fczB78nE+0JMM09fxp3YztpZPbsLCyFAZpY798kSjjq6H5C2"
    "t1g7X9bkcePa1mLqFMgY3rpqtbvjET+bv3l+tlYvuNG15S580u793Hjo/wLHGHQu2w/j3wbdiwz+nCFZflpiBBAdOnP6Zf/J"
    "Xeby8u1u6O0uvN22lu6abVMsFEZMaDUEEl+xHdpPG/dzPfcmACJlYW2BAhnA0ggoBRgMcksgIQcggjiEn32HLNiWzqbBYitl"
    "MY44nzw7FkCJrcE3D5QjsADUs/bIxCG9vtHvjn+5H/6DZHv9PhzG+eQs4D1v8/hE2rft24f++Jd/PPT6nYvMfaZARi5uJ7wu"
    "vBqgWhI6/qV/PyY+bM+fuQ6npyU/re09Ayol1vPkMyWtzdxmbzlzZ7oGirSdBSD+yXPhwzNvhYQH35PfZ9RS8tdnZVicP2ct"
    "l3MX2Y/TL+cQH/4BgCkA31KStxG4H86LhWYZf2M7ICX+/Z//5hVBUdUExXq1SSEn2vf9q97wrtvRhEXr9vb+l5DEkJQ1oqdF"
    "oI3g06QOLHwJ2Kiun8SRsh97TLb8pVQHkdAaDG573dE5AOhRlSSBfMiGuDxHPruwnM63K2cBn4fNBPj0FirLR2QL/o1TIV1o"
    "aq09SgRU4ERIhn914cHDhvoe3wAFrRBAsGugCM75YkMGUC3KC51P1ihrP1urlbVYc2EE5wcgrA35oi+BCoh2UUpd3V+R3zfl"
    "YqlCVh4geSUk9bOzWMMm18A4azJ54UTjLtypay2ARdtygwE5fbaAntg35y/0E/Bn/i7sYer4PkAmC6vAz5Zrk78W84BNawUy"
    "1HZ9UA3eZ2SK7v0lycDOMrCZGd0xoINjgX4JCMH38MeVw3iKWBtgMVjcdmbIJPxIGqW3b7utfkE8aczcL+ckBjLmJUFirwvW"
    "Zac05LkMfq4s5ZxclHOqKucA6yz9h/XKmjoPANGVS3//v8ETyFrWegP//nDnrHG1ZK0NlGk/WMg65WK5ZhTrRrE5LlbPzdq5"
    "Wfp/i8Vz+pj/ZC0dvoBTt5uNpmNYVrlqVJpm0ZhMTbAUStNybVKc1Cd2A9/wlg8L6xk59f7yhlzBZoGEYljxGkSTg+jzkW2p"
    "kLXmDlX0DuXFjK9IO6nz10/WmuJi3OsrWtr2HCYPGcnLB2a6OBV8vOAbK5ArNA4UlrS96QbplYlc5HaNf3FRwH+xYhYbZbNa"
    "JllU2MggBIQwSNdMuUquup3usHVLxq1fSa/zlwxl7Qy8+pfgxUyOf5ttJK/vEz+TvWuP+w+9Tg4/CLLQC14WqmLiAffDs/B3"
    "pF6gmzWIDGDFYH8XwVu5vICLT+5a4/aNzvRTphwB7PCEQR8g3V/b3cG4B9QrNMk5yfRmKsBwq4Ac2CD9bUi+sJM4hccCcdcM"
    "OYAofjJyRl9BUUrG1heAVWSLXCoCBJwvU2fJ5I3ANSqiF8GfviPWty+o6ihkUHkB13srhy2rKU+UR1J5otJau88O8M/zcgdP"
    "gLBerAWN78EVFqV7JHC05mZz65E8uz6jVyNEzs8bn59GkHEiCX+gXL9ax7NzuX5eDra+9ry5/7DxUb3++wOQOnzrAVSm/cAk"
    "xeTlYTZfPNjTKTw8w+UfgCIelt5qbc0fqCp8eAZpM4/7s9jhA1cp4hn/4REe5Ebmh/8wYCCDM7mCwuiLM90gcNhvUvkk4jBo"
    "7dLj/CfRpXA+MYkKJ535uw5eACGzma8LK+szyeosm4s9UWHi2S+FjrW2Cm1ca8R/Pey2H9BwLXBiv5DrwDLj+/vbh/t/nO9A"
    "gXhsByoSH4tBifZsLGpQYwnypvZS8Rzkt2PQf3cFF/rkb+Qe7K8VrE+GgE1fokRacP/WVtw8T5wVaDX43WZC8c9AzVUXf3cz"
    "nTqO/ZUJFPeLRPkg9Sndl+9/UA4ESne6cpecqdNCCEwqn0mCBxQ4D74z/QDSpVCpV/6bfytdbjfMSqM6KRvFWXkCUsuagtSa"
    "FA271Kza5qRaadjF19PlIHwz3SG8vvoEtrefQSeDeg9oXFHhjrLZXXzy5p8c0n12wKZbTMEz9EBd+vwt5pkM5hZz4PhidO37"
    "q6teG73NwajdQc4rlXL5wHC0NytUBTZ9uFlsNOo39yS79F/AF0Az0Fq+nE2cJ+uT661Alz451nz9xBewXetx4fnwIvz3qmKW"
    "iyTbsv8HpDW1cTsuGKUAmlxgzWOcIbzBYqlYhe3Bt4s14/7jeNAa98CVIIPRb+2be1APw9bgt1yBqS+mq4Q3gPCD1RwJlJUK"
    "FGbYClgWfiKpbCl2GrDyMz3bevLInWNTK4KdnCzn4NRRXcqWhbXwV0Shf3qqu+tLct1rk5u7e/LXUpGASfHXcr1KBoDrVueu"
    "N86Tf35sDcfd4e1vpDcgg98QRbed0UOnO2pzADN3C4mOrvnr3/t318b18P7jAPzS0cdhq9/ukvb93V1vNEKzpDW6z+QjLvPc"
    "A1Axf+fO8oHfnkDnrYF2wW1rdR5G49a4C5bRXUtBFPUkALSTjY+ETGGSaXvPAOkpujdgwmcSUfIZIOl74N6vIkBNhikDabJN"
    "koA+yj7tsco/+GO2i9wIBO4tPm3mvgusM2bUzAxI4i9BPc7cKaGk7lrgQEzJGoQro13QmbhDMKi4i0tdy/ZgTHmFckq9SMAt"
    "8t1nd26t8KMGDY/As/QRjY+fwH8LsZvktR4YmqvlymExlrPuFwSHpO0h0wr4z0Gw07N75E2QrpYMdP0NRRH7gH+GChjjLlfw"
    "dziDgYiHr/ik4zxihPAWkDMHdH9+csGoYqjxNuslLIWn19kfYIh7YJ/hzJWn2IFDM7yllATk44LD3bEVYuNrRnhou0gAl+Am"
    "l0yEAecL4elTypAic2950L8ZDHtb5IDylyNEQIeLgLPTCAMB40FngNKfvtq6/aV1WSyZO4DHjs1One077uPTxFs9eZ5NbthJ"
    "B3hSpNEnJOieDz/auRimFnECS5c/xsQC25voUoU/vcFYEcaIOfxwNUZzckv0A3sjEZHCxI9rb8FlCvmXz0jgRjSAIskPJoWA"
    "N+mrWRYu7wEvrtD1pHBzfbTwc3BysbOzELSCwwYIOYSSNFEBv0D+Btnj+mu0cEC8CgFM7hdzELI0XPbosODBbLOi8ooHfTCG"
    "IHCCmxRbLCS7qIl4y+ylKVrgWPnPaY0tiz2tG1mHibod9tbX1AGCiC/brXFEBIOha5BRCwTTZWAEcvaHP2wmYJcjMbaAURxm"
    "pElxjn4nLpKkWKgA32bXMfhLtRpAnungLCPJzzSOryhsh1w1KoUvOVTPfAmp7fezDblt3rm7aQtSscCZswCIAFigVLRwPOSI"
    "kF1+BkBeWitGEnCSO8DmoyMFZxsDVFlcNhdEchRtHBzGgmfnLnDEwrWCU6ycx80cg+EvwesaKUuNZymWYMT8G3pfJpvVS56Z"
    "fkgDzw66pbgFfLoFWF9Z7gLMIkd5KE7BAYHES3d8OEbVbyULNLMoa9D8HtUxM3wJ10XZkQ07OueEeTnbqWkRdhQ+ub67Rjmp"
    "AJkiW8CPSS/cOzwjAieKTgmicCILtPZUsgrZtJKW9pNZzxNANnxYl1Z8CTiVJR5Qv3q0c7cUMPajzuSh7p/qlWTaT+7cXjkL"
    "X1EdIOsYTWZAulgksjc9FZeEaAkPXXKHYto8uew8L9cvyjvAeX8YoB5xz/bKWxpAfMyFoulkDGsD6/D/a9boc2atRWPOE1ji"
    "jxxHE08BrigXR05ylkj+FIn5JORuIRSmHqWWjJCLFW9qYaIzsC/TWgOusHRVjXGIncPJ1XpBsqAxe2QIZHxRCYB0Nxh/vONf"
    "nbk0CREkBDPt3hnuD7eLWfXgkNJYyu22dEkIfEhq9ma6LuxrF2byulGzy0QN4+2ctNuXgcLZ58skK1QEVR5pUInMSVS0UYF9"
    "OLio0mUnAFQ4K45O3OhmgRUNUTuiTZOfuXiZmVZSbgFna3h3M2KlE+PR6Hi45lMBFmyAGLD21lJwJ0EwoiyDNLtUjCDGjZCX"
    "PmNmnhOYjjkqk/BbDAKAGQRASCruALUoM3qyVGhnWnOADH7az6hRBDLFLI0BUDqznZkFVqu6/oKWWLCsBE0FC1UF5MC+4nIK"
    "wz8C0HlpRPG8mKMpcJo1Z1mtPK07YZ4Ey27K9JGSG3NtBmtMjYk0IRA0BVoQAgpeCCqnyGek/2eHpj6pGg5OTGZwwIk1/UMH"
    "Xqj4JEiRU13s2Nw6wJqGL2t2/v2JmVvzlJq5AiVX/7xpnw1DImOLxM+moXkt6rCHm3haxbCfDNoGnJBxxOyQtfXFW3jPL9JI"
    "KldLoyL+51eSvfHAQeYyqoVpC2Gn5A40aLhKFzvarVkClKTTKYEVAMAYOrD0MwUZDW6cmDBOFfU+iMwUkzJsOOg1RWDcPL+x"
    "BQGgZ9DEWqXAD5N/XAV4If7UWVgg/tRA2RMVdPJ56f7KM678vPoAB4EPMsOhxW0KcaI+OQtjipWKLj0X49yeEjuC7fLTHlwB"
    "sEcuTVYADIUfraTVlIzY957SH3687RrFYukVU/vt+36nh4Uprdu4DP+WdDxNCmKc8oK5dvB69OlbkAW9tfPs81/8u/ifQpBN"
    "TP1Kb8Be4RZM6vdG3VGX7TFFQuCwqgQZrr3YX4Id9sXr4fXwod+6616kEXgHnkqm9y7uWrHglo8eihv6HgVcrw8wGfz9rPvr"
    "4Lbf+jsZjm7HI4qgM0AQ+fXub4NfzzqtcYtcte56t7/9RO5aI3hlRDrd62G3S267P3dvDzxo5yj0bV2Zpk0uRMpElB4UUFM+"
    "eLMHoSkvdC0Zuyr9oSOiJr3FzEMQtm/v8BMM/jSMsn1LtP4bg7XsxDxCu+MYOykhkcd3CYe356Fvjg5Twu4Awk7E9UHSYMAl"
    "zlZDFRanDhnTYDM0dBaPfkHxxQpaYeF3VL+laOI0tVmluNqs/w0qkUb8bVqnzN1G+sQna75x6C0gMNrxHx/6Hlg00pILh1bz"
    "oWhtnhYWqFmUmbdZ2HrOAj7LhTasH4htSXdYpiQoT2pQ9uhOHfpf5Ujwim6M4RWnDZ5DXkpAEfj4wEKi8fbGVg7Z14DY33D4"
    "8J///idApmaVwf83P2Ct2YFolVkBQCcrblGSkz+pzkFMckQNDsH3pE0AS6e0CqTQxcq4Q8RuoBlwhVYY9Yn1OgcSQlRbbFcq"
    "AUhS6rftqK4fjOpBfPUOoHZG/Twtf+vo+Vu1xIhLElk/syWLSWuMcgl8fixLKyVTx6LyldiychK2PGElSywnazyMbjO3GOkt"
    "LGkzHsLZnaM4O4TvSO3S0fzbOSn/cqBtJYjawQQR4E6x0mNKi/Rima8kkcOeBfr3um8Rwm1SacxXENGRvW9FaONghHbU1HNc"
    "9U2WOlK5II3KS3mWWBW6ecZLceJBWitSICA0QvUmMv0SRBcLB0je/AfVw4OXhI+nG/fwB2Heh/AbKlo6EK27nc993M5TC/vS"
    "McIeuCm5lON/NivXx3uxtEamLSLgdy1ugyl5eIA/x3nhJKZVnCm+w97iFSJHs27wlfxJzPEYobAVn+Vj8ClrHvSK6nyM47SH"
    "o8QxcbL4QCx6U/O0OOIJHarglCfGeehQWxFf1RHf99YGv7FzkJJWimFCyb6CTIEFhZvsCqSowQonswpfxRBLChjuVuv6eVMR"
    "SsKnjotQHxmT3UYv10cI/lh6kbnMWJqRuTGZx2OFKkEmkLXBWH/fpMIriN4lxRSLzbekmEIQIsVimlChKnfHuZmpVdl87xSk"
    "F029T1IqFXVSSq2oQK0sQNGwwqWgeogqo6xaixRTtuRz7cTro85kxVRCMVHhQ9AjpnhehO8ry0XMClmkRLLBwtYM24+EduuL"
    "HiQ5Hbms70wYtzEpge2gNd+SS7cW/nwlN/5UjLjjaN8XW6ZyHkqlUwXkJX0o9fwooJUSHDWwF1cJw8vGvkdprteAvTmtqBHC"
    "o0htK62Ao/n9t0v42Vm5M+Uuz5beCJVGoVEtv11zhLo9KZanzsQw7ZpjVEpNx2jW7Clt7uLY8MeZXT9Zc4TYC5yuzzvI4fXN"
    "m/ZViyyfXnzaLSu4wfDM72+G73iIGxiykWCmYdZJ+7bVuyNX98M7vBbX7fTarVvlhiMWzzns/vgCy4NhK8A5/CoX/hF3YZSq"
    "xeJZ+25E/xG/J6WqT1yGy9xlSJbn084APjOHXuG05rysUjQhnBjKG0FNH61nnFsv3matXJEQnYS0vdFnrTXJ8ppHemFl7v7h"
    "xHZlypOMWSKj3nW/Nf447JL7KzLqDn/utXv9azK4+W0E/2r16WNm3F/ORh9pm7ghQZ7NRGM1IoYT3KH3tPZwlCbyxFlPCzlV"
    "KC8VGJ3FQZmAheUtl57PWg1a5OOlUaycgQRfu+sNIzAVKbuvDAcURzIfL4sVApzuzvHyApOPMdW0kgyU3Wo3eUjcQqTLlRXr"
    "00k72sF/saUjudhKKrhmppfBm6PKMc+u+Pq5n2gzzziOeAKBiOWilCHScIMVcyhOWz/R9p+p247A45VcqNI1bm1+x0pQCku2"
    "5uMaCLqLGdbikkvaAzRHQKA8sa4TiwDWYr0VNpzcSEJc814keLmC0x1ssJpTW5YiFq7xEOKD9BJ2jkw26zhuFQgcfbxUkSh7"
    "nkYPe3Ch7R5yWRbaDtg9BCRueQXhnVTXlr9edW2a/lm0m9xifZHMbwfWhykS4+LuuDUE0dJ1UjYF236g76wurJyyLqy8oy5s"
    "jzBCXLNjXeNF1DlvCEqv2QhtGNwMF7Ju4n0BVVauGCAV4/ImUgsz00IRrVLDomxdhZ0k7AXj+kjLssVrsklGU58BgSJolF8J"
    "euO/5kSCLwrGS6KtsLuTuIM0kY092HdHlkQ9aqonJQQSPZxyNDW6R0alHVieqIZ4lNuJ0NgzL+/il4CEMSOy51KTxtpUWTRs"
    "wGKj9g39lR8pZnqyPjkR66aX2YdAQiiPsaaOzZm+Fv7ehYd6v3QWaoPmwPlIdlVr4Kk2385TdZypXZ2VqobVrJeNilkqG7Ce"
    "Y9RLdbCI6rWZ3TiNpwpMYdB+D1NrMgfTPfj3w+cnAJN2iXbLTT3zkjXjp1gC5x9EDL0Zy/BZIK1+h5iX/IL8NmogSFy8sjAw"
    "NuW1Ov4Q+2SG1itKA/p6ODCLxVqj1Cg30CFjKFZxEb4lrPhJ4tqe2AS6THAoS+v+PhMtheOPqdweTtUNw2ylAFprD6CtNyvc"
    "pvKXCwmmtXpRXMIsqwItlwgf2GpGOgoqoDgiRKftLV3sC+TG+4x99ND5MVBQiAvPwTvPHtakWmCNbkDUnqeGEKhTWRv5PHEf"
    "N97GL5Bbz/sDv8HbSiudkPOsXbxJX5hg/IE1xeAbCnfkpe0zOBWIZxQghKGmtdWhAEygrwxFbjAbQCV9ZkCFMZmPB/Kzs06i"
    "MxRSiMByK0/Kvd3QZM8ldQiiweig3aCAE/zjcWU9i1jISr3fCc57i0Kw3MuxdjN8WsFEVLi4iy08JYqMI+jcqTNUcIYaSfL9"
    "YgjJx5IcTIdie6ig+I139obfDVrDca91S27uR4PeuHXb+1cL/SdGc0ltWwDcGYRDBkCJ11XdR1ZB7afg5SQCYBIPmRA+V26d"
    "ATZlrh/W/cXF8RNrLWfAsZ4hzJ7aLb+YkDh9M+099NlWD7/wTlz84tdz8bcdVPgIqtgxL9McX7xpo6XJ2QvoBAuz+E99oIQL"
    "jceOWbcUWTeBT78/172Y0nU3T3el61rX6OalsGRkNRrrRlICcdLq00rFvsdnHt0lcch2uoKvAmWFPKHE9bfprVS+0r67S/SC"
    "zKgXu8ODPQVgtpvXaUIDJwVA5WA3PobSkLzAzgNLm1tLchpMAIHTkNd2a/Lt6ShUV9JbwGan8w12eE5ZIACmFfAKWgKKwbWX"
    "VRVpmyQNaGpqTK0F4mfiKLOGLJ8anIci5XCpjwgIa5Atkv8/IfTvNoEPI4G0u8+flnreRSzmlvZzVyfOUY8raxqspwuvPPsb"
    "6QgvnEw3qxVLhzNfVldD8fGbWqVQq5beLn5TLFca02azYjiWA8Zcw64ZjVKxaUyrs4o5LdaadmPyliOV4mYkBt4A/66YFujz"
    "jwYx1syA5g0RrPgXZ3WmJLsFfWBHOPWuQj5F8pEnfweZLe2SMXgh3XN9DNtS2ZIyyU33WZTGb0pn05gDiCmAeRnp4L3q9Blv"
    "eracefUOBevudPxnWjC5eOTDLG/FsbafyUeFSDkBn2XzlM6wNJNiMDpxjpUSULkVoakOd445kvOhSZHy1+j+spXzNC6AKfhk"
    "KtpNQYh8hnNlMhfvVQ1yMo5YlA7b7po3D1ehrZYEqKdnnrM+4YdmkUipdFauylEBaqUJH24lxleB76yM5IrqSBozUgbW4Qvz"
    "tMhk/eApITHqYZiivf8obokYHZXIusxLD/QxgkvOJWFrqLQhTorDLDJ+MCxQiclp8ZFQBIWHFWmdsEVws3RWK5PUmc6Q/L3V"
    "78K3uhm9auEZ9Jm7pK0QlcmJDAiskGczn8uPLagCky1cNQ0g4BwJtcFCK3cNWlxHi+hFBifGEM1nh66OfM8GiOzEEZ2YqZEb"
    "x5KKIfyZRx7lB3sjJYaut1BGyE2dGEJHzH12WGYpHoMHB172UEQy8HLtsVbraCGxtBvPGoFCYnyNG8bSabpbaphqg8w4wDAe"
    "Ufh9wZbrUKNE41OWrMVph58UMqdT0VYaDAJRQVOxvy+G4i38i5Lb9Ym4ZIfFU3SGsLKywK31CeiNoicwBZWQuBBe/EolM6PB"
    "HkIygV8kbJSeJpZX6UxIf+ktWMDbY1oZO6q70w2OW+EreGzJxHUEYwj7h46oFAC1ASoDZ0UFP+Ue1hPP5+wDksJf0xUcC34U"
    "kMAm/GGeF8eHvzHIFK7gtdB6NHVu8xGDMw9HcuLhUJKLBw0cLst+UCYKd3v9rHbeXDwSwrvhMWP2dSo7dKip0q6tFC0aBMfb"
    "hnfRH/QO/ax8VVM84mPmKvoxMZuCgoriwVtKw0a+UAi9cOD2NIIPFopsNk/4rEe2IzRM6DY4cvJ4TtZzV172GGM+UEu40fWl"
    "JFy40z/wN2x88HTu+Y5UPFO8to6DLpkjSWmHe5J070BsCiQMovxHnsay7RVv48kaCqMaSIBlbxbaZSiQtcazrFf0fiX7TeQz"
    "p0CAtlYUB1cqMMWzkrWGzjPw9wjzN3n27zbKAgQu+/Ff7pJkryj+KsR2H921nwPY9b3PtDMxNoGlZhPnXQHqFVPkEuX894It"
    "cX2KfCn/QK1+UvDM7YwFG3SJ5xVpsphPlbVfWFN8EO2Hl0AYBarYFyupyiRZLsFZeyzs8cJPigOus+CE8IHJOcVyX+HdAjHY"
    "Pfp3TdjLo8ooiYEUJc0ZMX2exlnkm/pR8xREbFs6xYtN9sObyMM3KGrAEkn4htBpGpyTPxU3zF4ZO8BoBRh0+oeuyaODqlVu"
    "iPUYCm26jDvjG/GR6yhHoNGJ5o3S1zYCs0DDsCPh3jB+jfzps1Ttj9TPaVI/BzTa48PQr1PcrFeeZ23+dId/V+er1B+LG72a"
    "dvgrFgFY7iLsIqbYG8jFi1KpXClXzXqzkgIc/V/grUHnss2qUfHHdG919be6qd5q699qD/+8ya99UhLghelUyqJSA/r7wDek"
    "0+y538mvmxZS0zN+J5yOSA6nsOj0sfG44xuvBNtPjEQXKdIOuXrcVgMYmqdNpdlXgiy44ziMAf+FW8qkya2lhmE+tnYaR6Iz"
    "o2ECetdx5FUotLREoGawPTYzd8EixLli6IFl9RBNKEJj0AuhOIWKNgpCO4zGYfSAi7Ww5i/YlAqHEM3nMSEcSvx0ow9wBIw1"
    "pJOkXHRrL4Twd5suSBONisa5g+HA6JEYFdtPrW+2807pRxYndRanUiwUy2ZSFkepdzhhJqdmNmu1qmUb5VqzbFSKZs1olsym"
    "USuWas1KszmzJuWvUYn7DT1+aGxyD9DK2CQDJPWhFOnAJUKB/L4QXgMt8Gy8F6eh8j3Wi+meBn90D/P/oEW2mN1HrRdjkB+z"
    "3jsw1SspTfXK6erUWGT3Mb6GqNQ6a/XP9LoxWpdORUEl1FntVBVrPqiwJw9vCmfZd86Tq+Ryr1RilI+vDsJaYP4xsCynFkg2"
    "FjnFfboBLOJyVRlfBXOkk7Yufpn0TXO4RFOoclTfzHZ0y/tQBh0RG0DiRKQxxIqDiTP3Fo++1EgVkn1z8tgC8sNbW14fDGqd"
    "7E4E7NB2kgtJOZfKHbAWA2/JmK1FbPFJtn3T7ZPhx/vfPtI56/xqgrsgE9oQQPTriHo1sHl45ME0MT6FMgmM5Eal0WhUgr9w"
    "0R18I8LSMVty5bTGoD3HId5U2sBclp00lwQ5NXPuajUlso6IZBOcxhylwojQEgQpayaZ5U6b224NMfQS64fi1gvl0bPqSf6f"
    "C9x4LnUR7xFmVaKu4PF+mj1Hpn8CI1p42c4XoESSRSMqT9D0wf9tDwGg4s6L1sWCJjgKZIBej3rjCqzKF64qfZE346WlPjXq"
    "ee0H6haxGs8in2EOl2tZUcLFcoy8DKt92231C3G6mV48km/7coqo6ry8DthjDNET2qDbxbr5Q6x/ZbGuSfPeaA9pHnRVDMqc"
    "2B10EGYbMU9Du1pJsBkMpt44EOi4Vw0XB2gBjtJvSwHEVBjhRq8DPSCAh31stJq9ZxqV3iHVGUmFBXq8MA8HkbEBkLOKrYJi"
    "rSzeSMJvkQuV9xBs3HYJKaH62yxUy9U3rP62nVnRqUwNu1KrG5WKbRrWrGEalWKzXCzVJ5VSo3ii6m9e2ZxU/h1761i5m/qW"
    "V8uDe84Jbpq4zBJzyTlU0d2y2Z/YBPO1Pneb93TFe8Ki16x5bhJrjtT9ol7nl+Xx7DatIm8t2VE2dMDDCzHT04QMdvILKzqJ"
    "v4dwZvU7C2d+Z5G5asrIXPWESfTtLP065qCy+hvGTarvQYnGipZ47VkqFkqNxttm3azqDN41G0bJbFRQGjQNq+iYhl2bTMtm"
    "3a5P6+afPOv2Rkm6PTAh9ZYsBQxKMUkW/PrclltXyMk0cBJUafy+6M3Cmb6gSl8cjFU/vs+03w89+V3oyePjK9QADQIq1aQx"
    "QKI53CsqUx5KqX7FUMr3lbqqHpW62mE4FeIJovCeaGALXMuvBddkXvuTQNZ8PYqlUQnKqW9KtG8Pw8oPGB4Nwz+bN1UtF8w3"
    "bCTaqNfqNdOqGZVKsWRUbKdoNO1ZzahOZpNprdkoNSvWaUORYow4lXM1ftU+uANLSyHUAGBGdriMxvB8gg7LZO76T2q4bhpf"
    "7kHjkdkg5lcVdxfZ7UDgseBveGHOVi4E0idaaaKUZisvzpaFN7ZaagdHC/dAm/S6zNY78XpqP7ye1/R6aim9ntqrRwfNVkGw"
    "EhcaZktjO7wN+UoaD76UVdrBrIW+fWNTrPYe1B+VPPHqrlmomW9csT+xmtW6VZoZJRAZRsUqWYZl2yBFG81ZrTKr1aZ27ZuP"
    "HR6qOPY4fIpwXXw6nEXqCkF8LvTYnyI090NJfRdKag+fbMiNV3/qLVkHGVUVgb6g/RCTKo1/ksbvqxRCKXXmhsJz6cNXJ1RZ"
    "rxHvMls/CVMAP+iDWf3aIRm0APgnv2YwpvZeWjlusQLKpULJbLyd11u2QM017KpRm03pQLGKYYFLZTQbVtOqFSsNEM4n9npp"
    "Fw3ssObw/meiZZViZFJqq58D0RfIpWiBEeP1qo5qVWn0ljB8IdTWfVshT6jNouxMRp1YxYGlsy9o+ImxSD0yGiITbz1ntOEX"
    "Wuw+IjJ5Jxk2t0GBzaG2zx5YD5zmy3dij9R/2COvaY/UU9oj9dPMUxsEQkEVBaKGLRAJEaY6ka5sJ8suko18NPemHcjr70Jb"
    "Xm7xmRu1NwwRW9bEmZSnRaNRtk2jUq7OjEbJLBsNq2iVzUrVrlROc8M95llGVSQLJJQn1fWTvEv5sceo/y+lejlHWnQc7+hc"
    "9tsNLu/w+nT1Rs1F5jqTkyP9tGRvZOgq7/gVV4Yv+qlGm2G5vLMWnZ3WH//yj4dev3ORuRdfpeNgDPU9vgHZPJFzr2zEKjdk"
    "iB7i9AKNGFtEleRnawXksvbZoWhEfbo25Iu+BCpIZ5ylnr26vyK/b8rFUoWsPCDBlagMprNzRX2r6LUPGKMjB3O0z41icfAh"
    "jZZP+DfnL/QTQZ9+3pwVIJNFvJMljmv/azEP2LRWjw6xXd/CFpHYRbB7f0kysLMMDhSiOwZ0cCzQL4kafj5ap3/PGl162Plk"
    "hnf/+ZGCrckLQOJJY+Z+OScxkOFGTZ2iT6LBkOcy+LmyOGp7kTu4Zio9R/2omXpvhtBx/dUOmXm7V8eDtKul63eQcrW4bgeH"
    "rra7w9yhc4OlJL9P2dyHj1IV15bOSXCxijZQJcFN3Nyf1+TdI0800G+Rkuxi28XUJXwOwM8mmLP28MP7jwN+8YoPfFUNgMTx"
    "hvBUbF1YMM8xMgYtfJEs/yGG1vH0jNrjrnDiHyljxV0VpX8cnqx67UjZdDLJdDKhdDJ5tMXPiAY6U47oCQ+eCa4vu74WuVWb"
    "olItPki8PSgb34b6r6KZql/bjFzZbIdHQ+A7UbM24QOiOV60u67sBMwsLP1k+MdlMMLPZ0YGnSXxC76jRLQj/PX7IgAZdpAP"
    "rxt7XvaX0B6PquysRysQj+wrqczlVDJ9qS6lnopgDryFuo+wSCjr7W31xMSoUe2K9JZmF9o95rC63X6veSeDnHrWetJ1Zt4j"
    "oS16JDCNxmZ3otw8Q5l3hvKKbNWErGNlqAcD/PXRWtlzPp4k4qwqPSVdbcoxvEt9MO4kRUfNirnmYjUMCbyRAuzvLbXUHtj7"
    "SIWjzcj3rTPzKcOW9fMSyQayPhfcreW1eSJF4K4DeyzeGMuGr9ZLZYrrBSyeY1VF7Otlkg10Cfs6HUSgbEGZgsMmtIQZCa/1"
    "x/Gfz+4eK51Nkhqb4Aq292yByHncuLYFXzlFu5ID9Jn5Q5+9gj7rjeSsMJyxxbQKV2PUEKKNIWKYwxRJP7W7tD43/RQaD1fc"
    "U+slSMVTqL0Qe9kbhw15TtZwQT8hwT75UKMgboKGuwVFuwSRNhtpyV9QmTfcfiiORfPInyw4jPNGjmLUP6lPVYlrF07n1GNg"
    "GstJglwTD/YGqYFz8jMYjlReLDlJ8GdQ0bPsAA3CK/qDUf5PoUz6lrh2Vkap8wSj6NvD52rovO+FwtWFveIeT6gct1l6LCSM"
    "1BeZhxcb8GhHgx0Ip6hyNb6VEEdoe5FePHTcIE2aHNvfPp6V9maZPVkjf2S7+IGuSoKaESGKSkYluAWB5tf6Cajk8UnROpUc"
    "7xE1nycYQsggVDZzt4BSIvcz8G8hOTu6uf942+EVJFktNUNTKZJXeTu4XB6++Qebekidj0AI006cKw+OCHjdIoFB+jIxXJGy"
    "4bD2bltlVfWd55obpUK92HjLwbhWsVSrT4zybFo3KrPGxLCsRtMoN01z5lSsYrnSfL3BuFgw1Zp/tl58Akdjwajg50w+iPdu"
    "FvIt2nItKGnSvepggim/s0fFMueZuVpC1TgnLTqxr+09L+fAVnaO3WgSRRa0CBxZhw32W+FwLNrECZbkY83wUOGycZc7NOrB"
    "MjyFGpl6iwxPt+wfMVEyNQZlcjPY2jtJMja+XpLxO8siNVJmkRqnKZz6qPItGDUWo7xAH4T0icI0qVRF/rQfPUY9Nd7L9JCQ"
    "WEzWVYV6qYnnnaFsDT5A/TqEcc0uTiZOrWiUapOpUbGaDeC+qW3M6pNSsVKbWDUQTYA3XaH9vVgtNcolptAAGRS1ioeOP3ID"
    "SVqZkpb5z2Ee7nT7mDMDU8p2pyjnUDs5lu8tALc7x7azKX8rxyGg59Db4OPCDw2cGNGZw0GjTLVe9yc65nvfbJJBnC8W6Dsx"
    "xDqumIrHQrKj7vDnXrvXvyaDm99G8K9W/2z0kZZ3DUm/ddelva+d5yX6M6ICC/W7MQfDdE5GP7fZCFZ6MKH7+PPwr/696jzI"
    "7cPBzBz1fCJpL8XoFW1UPzvMPAaFqQ26xfc5RvKyNgvE9bj76/isc3/X6vXJ9cdep9Vvdwl1aP3g0vJS9fFiXI88iXEvGAxi"
    "3AhxDmNbZlwOOpdmux7ZNPrd8S/3w39syxQguWCuILgXHYqO5uP8AbVLM6WyIFbCjH0WBtOCpdIJ2drd0U+K7Ira2MiN8UxS"
    "KCwTzKHkJMvomMIzH+ydflcyZyYu7JcBJkDXZmUhvzNGgE+re5FxvvHW5pa4GzloFKXywkbut8JsqUT05P7ZnrOTXARPsnVw"
    "KJwdSz48ZDFjhseH82KhWc0H/sF/84p0rGrSkSmwXcIROOaqN7zrdjQJ2bq9vf8lJCZ/VIz+qBj93ipGQ5xTVjkHWGfpP+Ak"
    "eecBILpyHWbHpvW9NVMl3vdugu90XqrF3opy6naz0XTAXytXjUrTLBoTsBKNSWlark2Kk/rEbrye7610JeYaEQf1BbKXXkGi"
    "GWYxoZs9MNOFmeBj0Yy7QK7E7Se50HQTqGjkdo1/cVHAf7FSNku1ZhljZU/eZ2QQblRkylVy1e10h61bMm79Snqdv2Qoa2f4"
    "vFj+YibHv802ktf3iZ/J8lmBOWmoyJeFQp14OMoeJ0Bi6IrZdFNkxWB/F8FbubyAC3gPrXH7Rmd6HlzL0Mm2Bn2AdH9tdwfo"
    "TcrMyTmoMK35PG4VkIPzt/G3IfnCTuIUQHO5aznQnZ+MnLGB5yBK+QTGyBYDPep8mTpsWrvENQsYcv70HRmJvKCqo5AJrqvt"
    "6gm9JZwRZgn90lh6ppDhjDFm4ej082fXZ+RqhKj5eeOL1jKcihMpeFtIpHFe1bb+LYZEoqGQVH5YmlAId6MiMZDU8wR0js0d"
    "OoAjmDbNV/qOqnmpwVQ8BwHuGPTfXcGGPvkbuQcDbAXrE7z97qeM1RTjYjXffxwiLYRigxOmWTDrtX0C6ccpc7thVhrVSdko"
    "zsoTkFvWFOTWpGjYpWbVNifVSsMuvp0y54HvBagW23ryyJ1jUzX25FhzUHDLubVgwjwYEwC/Igr8aWz+7vqSXPfa5Obunvy1"
    "VCSg0/5arlfJoDskrc5db5wn//zYGo67w9vfSG9ABr/hHOzbzojeBOCqh9n7eGi65q9/799dG6wyvNcffRxS77x9f3fXG41Q"
    "L7ZG95l8xGOa05G2VEzfWT7g+wmk7hpgB35Dq/MwGrfG3XPQcDk9TIGSfoIRPPSl8fNt7/nZWU3RvAYTMsMvYDP9JQco4cgB"
    "AKTvYRwiAtNkkO7wJQs/kT2xhwhgHwJ7PBl5KU6az0i4RFCtoOxgtMtMjUVfZfNfwEf2Nys0cjCcCMoZlUkOTi52doafwP3w"
    "ZHlw2ACeS+sFSWCBsS2GG93lE/Yls+jQeMTD986QpJDCgCQVUMgd5UJ377XL9Elwz6TGKU1tgQ72n4GpV58wG5rhKSnlij8u"
    "5S4+efNPmGaiT/v8aXZ427UeF57PKOKqYhbAGM627P8Bc4aeueOC04YsQt3WO/cLsAj8br1ZTSxeuNp99pjspKEr2OBmus7J"
    "4gTx1SXIndXmGT09tmDw5SzDLRt6NAGDtFEpfMmJHoHgONqbFUIPZPINGI61mzvRPgxjyCvnyVlgfT4lgc0CY6GjzRIViIQM"
    "q8dzneeAeS/brXFEHoHENMioBRi9dJ6sT663AozeMPTAHzYTEPB46hYwgkRwlLdxhDo9Ngu3AMgE0BlpyXaB23D7PNnM8WM6"
    "VkW/GQCseICvFQcueFpALJsSVtTB6ACouHQFqmRo5kdgrA+k/7GPweW7bn8MftNNt3U7viFt+Kk7RBl9PyJVU5GV4m0Uso/e"
    "6uXU4JbACMj7lyd37sQyMkZkaQhZeW/pTv8wNssze+UtDW82YwoBl2eZGZLl/9esnZm1Vo630/DdZ/RoeKTC502r4dTWSlSi"
    "0Zj5Gr6HpEABorAmL1qO7p9FpFk86hDJ3r8ZDHtbRLrylxOo4rNB5xRKWWqQlYdSBNxLxgu3v7QuiyUzg/VWTBPBU1hDLOG3"
    "IOzA2b7jPj5NvNWT59mClgZ4SHhp+IRM0fPhRzsXVqwil69pf4NFjAVgFE0iPkhf34WdTLc9DsQ0yXaRWIBVvcWnzZzyI5K2"
    "tXwRbVeC2UpHsrMf5uewwD+ZvFe0f0RgWMoG7+AzAZu34SdntY21EXRbAJYkWul74FaiDTV/EUrQJ+3BmAITjZo/HNIsNupF"
    "XcKKJyeBTOKkMpWH8DmUxccZUcAnt4l04KTO3U1bCHRxAWbtofhAO9BDsuqCJfEI3u8LGXreMzkjHWdprRh2ABx31sJ6dCRp"
    "tzGMlMVlc0G8RalrCSAAOLDmLgijhWsZqI7dmTvFTBAInjVKZPl6PDgsxVyO2Mh3rUBdROQJEHw8W+HDMTqCEswe0CdZzhU5"
    "nAG2i9Z45SNs05GQXiGkP7m+u06kRF3AUkRK7c4g5a2CUCdHtRIC4xmynekgGWynqgPcTn8HSaW0/kJkpVuBA8Gzo32VPJVL"
    "VNOvT25+CLpIKbK2GgghXEuhYS1ePrOXlAQDq+/aAnZmuksLPmKiMRoMnKPAElCUYzqlyARxxKU6RF8zXMf5ciFXjhJO6IyJ"
    "wE2GYJhnQisKLX+0VxtGxzlpty8DObuHE4vtxLlkpDIzDYaQTomKDSrVtsiRHeBAOWKxEwDXOCte8xFwTNRWbtPMXE51BHaA"
    "U2S5nywVopnWHMCAiTVfrY200GqfPhkAkjPbmVmb+VpdH0iAiEAuTZ5FurvL6uTieTFyISxPyxVELQkGz2WQXUkg8ObtbCYC"
    "z6UAYD3afE9qs+CFoHCMGdLPzppb7OohQcDO5xNr+ocOr6BvCnx45YgEv6Bt0NPKRWeWAP7Chk8eQKut4d3NiNlS49HoBESr"
    "0GI+bM8qoRGkXSZXjFR0nguMZDWUksDOWiEqtvVGUEXemGOdChVwOhQUu0qlQl4xhtlldwYWE0r/CIBA6LDomtrvcOLMPRwv"
    "sPZ2i3XY2NCB11gwiLpNKWR7Ng2mNOdo32DliQKoiqWeNvC1d8xrt9hzsbBaAlmTXPH2i3wNTbz5/ABGu7Ke3Tnrn9B+cuc2"
    "kA+5+udN+2wY0hW7sPzgzR4Els+jOP4m1Px+qmcbaKK4cVUxohoLrq258EE10g6TXLrU4psHZ3f3yJLI7O5QOF9KwoSF4KNp"
    "k+89d8tLiUuvmMN9hZ5ZIsp0sT83HHbF8Xp4PXzA6tSLNMx72DeCtNLFXUskCwth+XKhS5cjbmxiwJtBkYddY2+m0R86wuHq"
    "gW74d/E/hfbtXeeh12l32AKtzt8/jsboFZJOb3Q/7AD473q/dvCn8cfhJene3dMSFKSFj+1x7Jdu4Xy9tfPs81/gd3oD8QmA"
    "8IC078D5xGrh++GYjIY/tymiO73u3U/ktgtmwvim1SeXrfZN9/Ye/tC9Hna75PbnFJf00kEjcY+9zgX3bFO/NxgpZ9viWO+4"
    "MNgZ0K+LkOj2pw/d66g76rK93rQH7ZFB+vd9Y3zTHbYGv0kckOzdTW7X9Uadxr8FJj8SN8d96HQEftw+9kLwIcKtcxQ2qe/I"
    "1JbiOha0YrEDReGozTeWUrJ/Z9evSilLekqnm6PR94jQ8qmqGH6SQeItqRb4sFTC8I2UyJJCBEtwDiG8QGDhCq1Ik42kMx17"
    "NT3Y+FYhGYAkpdhNbsxQYo21D5zqMeAYZMUYio1Cgw6qR3AKrISNIrQydbMohKek8opToimyqa2gbhwM6o4a/Y7L1GVZ2QYv"
    "uRA1IaLeQnmMl1WMYlLxlzfGqMXdMK1yAT0xbkXAXgI7IrAI6R9oVF60fZC2FfxBWFfhG5F65cqBeNllrPY6+1p1+1iO2xF+"
    "+MQcbCkiygFkiknPrPj5SJo1j3FjkRBDVwwrZDYLW0+LJmBT2j/Uf9thAaXGsTjEgdjdF01bDbvTIbZ6DGLjojEHlC5srVnQ"
    "EhG0rwvzGeidK+k1vL12DXf2CxeofGM6lANtKy3UjqGFbnucxN4qLyvhwH1YWnoajKWP8jWkt4Bf2OUvhNCsVr4ci+F9uT+A"
    "wd6O73a8V47BuzSWsbiF1wloA8lEhYJSsMGeoviWwVJ4l5Ys7Cu/d9i4fEtHc2Pwlf2F9Vbgl49iumHAcxENqnNhRKMy7isQ"
    "Vtwo8ua7wtoAT1opUTZzhRPz5V5oP4qJh8fy8P7K+U0Z+vAumYESjksYxZZSfCXfNj7AvNuXimx+N/4TPnVcLP7ASJQE2xYC"
    "uD7CXD+AAGSWTGbpWLkHqHr3kbUjOJBGOt8EjfACnO+OVDopSKVYbMa1s0xBJg6rg2B1MUGlCquQUeteYkpkRPMWXn5zJgty"
    "EgpX2HAfGTllw6vFctH2RKIghmSDha3ZWnYedIILcso6qabAKaFc0bpwK3hLZkpO1PgvuOafspItYXqvHG4ZruZAgAZIPYA1"
    "T8VZeqHMV2SxrUgsnspAllE8XgfCyqBXavGI6ggr5U3s1BivECH/70XX6gVIb47iAF6vp3CLpfJpFW6yspUXo44rR/l+Lbbt"
    "lU/v0p4rlt5Fozza+Vq5mLHlNnqzUqjVK293Hb1uT4rlqTMxTLtGZy87RrNmT2lDDceGP87s+smuo8fed3PpzZkJvfuVuWlf"
    "tcjy6cWnDYqUi3f81lu4ohG796ywzSurbGyYddK+bfXuyNX98I4Y4EN3eu3WrXLtGGvaHHYLaYGVxrAPYBt+KQf/iFswStVi"
    "8ax9N6L/iN+QUkhHgwqsRxT8l7Z5uyCZuwytIORZxpzS0x6I3dDfGX28VN7LiY5FSqkfEOjMofeirflZ3IYIQJtNMMdLpeTj"
    "pVGsnLkLf+2uN7zDZbBv0QWLD8KzXX85x9aW2rVHCQkCf/M2a1bbOPG+0GbV2BVwCQjG97H2NB/c78hH0xzhdj554qynhVxe"
    "R0zsuVhZa4qbkwElkczHy2IFr92wQByTezEVwBLDCnS18ByJW4h0uWXD2jvGID7LsX42UBbmmb1ML4OjrhTEnF3x9XOskaOC"
    "Idau8QmkHNbHp6Ny2uTKXwKIfH4XDd4Y0DEswV44WNmW4O89ktWI5QwPnmP9F+OpFP5WyanlpLKzpCVKjSV8xd1bRjzlSsso"
    "V/5O2YMa6C5Ge1izSWziBDIeb0WwxkScUjTyZkjha66w/eNGUhplLLT4geL4u7DTai7aS5Nk9bEobCChcmuLXvjF5Vxfo5A8"
    "r2hWwHVwpeseAlhWug6CWwvyxsI7qWgtf72K1pTNiS6S+e+IGaZcfFzcHbeG4FC6zh4jXy6SzvT9VU6VU1ZOlU/TuDoiq5nk"
    "s+LUaFbIw1y8plPVLIZu5BJMbGZR6WLTxsDoQF3DbQWcJsd0TlSZyclvnMzweMqvBNXwX3NE45YE6ySSR8hlSbTfTjWZDxhw"
    "RwmAesxUT8rTJzoi5WgdyB6ZhRC6RK+DiFHH0SduqEhaUebDqPpZt0lIlurrAlONbFQA78QSXB1/svDKbsha6WX2oY4QvmOs"
    "o6Onu7wSAsvvwZO8XzoLzdyS7b0TXcpKsVCuNd/Oo3ScqV2dlaqG1ayXjYpZKhsN03SMeqkOBk29NrMbp/EogSuMIOJ7Hhmw"
    "od2W3XJb0GyxRuxsRh1gEu+2w98YPtmwEbPFb11vowaCxAUOyXqzwnGc6l+Az8zLDL9RplwLleGs7PVwUIT/1OqNJt7Cxyab"
    "GfpVbbRPsHNsGEqdmOeJ+7jxNqyNx9Lz0B1GiYvNbdOeDeTGtfuJjzHhp/Dp9WDNv46eKLwr5o2ho4ebF/2ug3QGucVeoQ7t"
    "pckbtBuhJXj/9f3Qwq7osZ1JBFDk4h6p1yWe3daWFIekbqOUyxRbutyDUnx+BZvpb3nXkD/E90+pRhKKSidE3LgkKtcmTktf"
    "hwDFnGU4lKWNkZ2Jvrzxx2RJTxeJr0AusQ0utjXxRfgkw4GgbABbmwSNMbLhbcjvsfXx2OZlLgkPgjzKrTwp93bjgz2X1KuF"
    "pifktfNwq1d2e112AwcLrNxioz57fAYQ74Q/QYrGttCfVPjxhrzRfcWSR1hvaDQir9Ey4sZmDQiCDKZQMnA8ZZMpqI8tovOz"
    "winKuvodVJnQ4S0irE+wUZ5uk3tVGkWrWMZuCLBpdtFfa7Ae+V68PNmN8MyOLiy0G8ihfvoeam2rn154J4568StfPU04qPAT"
    "lF9d0DFqB/UPjhn3cUFnHx65Xldfr3vkem19f+1hGnSL5SlXiD+OAQEXlzffn9dfTOn1m6e7L3WtSDeqs5j4LIGcavVp0WHf"
    "46N/7pJ4cDvlshGA4UmKSetvE43pp9rusbtEd8s8yl9OgCvYg2CncGMmkOwSMqeB7nar8+3BaO5VpHQSCFymh8CWqd17ASB2"
    "OtvQsTcL21pIC1lp4PYYppEToZ8Zr69zyC1YrhzMLCACwCYFWQCmkWKosvsFLjMUVeMs6i4pLCZWKpwImrtNtldhqD2V33bU"
    "vIuw0S1tih5EV4bMv8maBh1QSni54t9IR7iBZLpZrViGnUUqdEUWH2oqlcqFcqX6drGmYrnSmDabFcOxHMuoNOya0SgVm8a0"
    "OquY02KtaTcmbzkZRy7ZZbP8xvS2gfQH2Xc5LonPPxoEhDMDmpNEuOJfnNVZzEDtUGtZNatJxQN1qWKSm3nCesTxeU+gP6n1"
    "qLxPy3EwsKO/GT9ETp1WyL+InjF1IAeRuBDN9NJGzODK6aO6lsp5lWlfezbE/0zrPRePfOLirfjW9g/5CAZK/PgsyzefYQkv"
    "xVl0VBhL01D5FKGiDnfbOVrzstZDwzaLnsQNIUPVg8n/ZBraTT+IdoZtZbxS4PzmY3LgnCIorNbR4fR6uYgCCRYR1Oe00Pw+"
    "ZvDxjPjPUklWeIgBREpgRMwi8mbqfKVI10Nr4m3W6vQxfGGeFsGbBZsC6fJwh4Zx2qmQYp2GIVmtzVob1kz8qbOwVq4XzHnm"
    "g+g4Byggw2N/xi6CzOfHOml9KB0b06cXMoYYKeBQiyGUoZ5CLAY4QajD8pVqIXpUg2+Uj3XjzT23g0vsfeLISRqfXEsASQDu"
    "4CjKHgJbRlGuPdwQJX6e+uR+MAjuKW9dysY0UuwwIagOblIgXPh9wZbrUO2tUTSuIqJ4ARfSKVArAYCZzlS0oOr3xVC8hX9R"
    "+vChu8LuqPER3pqOECAPomiBoaRkBgSby5mXtILtCw6jw5UTNkpPEytKguogygEe014Yw3Wnm7klV/DYkonriO54wlBg0885"
    "QG2AysBZsQoaeMhkk9B9HlIFCvfXdAUcli4hAUiTn7J1cYp/Y5ApXMFrofW0aa8zDycQ4uFo81r+oIGjTtkPhnKgXj+rnTcX"
    "j4Twbnjcl32d5ut1qKn6o63UDBqkTLM0+i7oYNzDPitf1US0+BhObw1/TPTyp6CiePCWUkfLFwqhFw7cnkbwwUKRzeYJH23H"
    "doTqnG4jK2rA4Jw5mkiQV2nGKI611AhdX8r2hTv9A3/j88JLz3ekEppiiSROgZk4knaYCGV7B2JTIGEQ5T/yNJZtr3h7UNb0"
    "HOVyAix7s9AuZTxfDk0Evl7R5qrsN5HPnAIB2lpRHFypwBTPStYaOs/A3yNMv+TZv9soCxC47Md/uUuSvaL4qxDbfXTXfg5g"
    "1/c+07bC2EecGhicdwWoV7ytrEA5/71gS1yfIl/KP7AHPil45lfwFmywH56XG5FWzKfK2i+sKT4Ii7CUKRNGgYb0xUqqMkmW"
    "S3DWHpvt/cJPikN7s72FmCubU4bRrrDgP3u/oWXH0b9rwl4eVfrwBlKUnDbMd8l8fvmmftQ8BRHblk7xYpP98Cby8A2KGhzV"
    "Ef8NodM0OCd/io7S5WeOfK7AaQUYdPqHrsmjc3lVboi1swttuow74xvxkesoR9CWwp8ddQ57BGaBhmFHwr1hZBj5E91r+OuP"
    "PM5XaSGqzH6+PqKJZtjrvdBd3sNWjps2eegZQeRdlGr1Zq3SKDXKxyyE3U+7cxd0TXBb6mbzDBqQtwLYr1L0POTzUdVMSk2z"
    "XK6Xamb54NRTHzj0ojMkf2/1u6Ccun/eHNQe4d9uNAqTFeEeHorRI0ps5jf3H0XJy64udJJX0BsLB3mT4xUs5HtsuOv4BibB"
    "9hMjvUWKPi0I31vAKafzDc45SVXmqTruagiFYueVQLxPSChNViM1HGMzNnR4NLM3JqCyHUeOVUYjTcQwtIK2iF0lq7fCpkdW"
    "kQ05HlSSZjQdOk8sMtuARaJFtQk/M53Vifo/ZDZFJvmwcFNcfEm4qS6WWq8RzZa86GTNmWHAqtlkzEhMTsiqBJFnjrVw+3lA"
    "F9gAP6JvXpTU0CHjCkESe0PvuM9dsLxZ3ykVjHKrApqUxSkqHuDbGIhRdNRulaa9ECJOnlt4hUDrkQQr9r6XetsuIko/kkGp"
    "k0E1s9AsJQ5WVuosTpgPqpnNWq1q2UYZKNWoFM2a0SyZTaNWBNKtNJsza1L+GrXH39Djh0Zu9wCtjNwyQFIPUxEPXCQUyO8L"
    "4VPR6d2N9+JSVf4MpXEpPLF3XV/3nfkmlZS+SeUN6uPOWv0zvXSNNkaiQqBCk7zC4Akk2amK53xQYk8eHTapdwtiXz9PLt/L"
    "vVLxVz6+rGdN5MfAmJ5aIOmSeh1xW1At6hEtqGibSN234Be7+uNf/vHQ66Npdx9pj64LbCavUx1fX1r/eKJlVTmqzTJLrzwe"
    "RmyBA4ED6U5EZCHCTy7YzPJdaF1Dc29bZFg5qhPnUcCPUPKJ4D9ENgkmxYnPZb9F1ua8G4g6SgN52omAu0VYSIHNNli4Dv2n"
    "gbhge4ZTV8HBXOUQmHwUOq+9KFfAssfiB3wDY3RTb755XgQPGmKsWCjiwusbIkIh5klYi9cYyHuIO7y4BNimvfqbANNBKNUf"
    "FwpTTmpQ8FJpGhKlOATVeomRmimAweMUuKIsv+SuhV62kI2t/aDA2IMSjzDYtksD82BpsIfGL8RL4MK7FsEHywM60pltVDQ2"
    "FK64Inu3ECkXtd8qs14HrKrUo7GjhfnL+eL6a383i17Hvh3PmeH4qeWTpbMiyXyaukL81bi08h5CUdvuysRHleqlQukN64tt"
    "Z1Z0KlPDrtTqRqVim4Y1a5hGpdgsF0v1SaXUKJ6ovphPcg6Vt/IrqsF9VwVGot2TiDGDZBB9HzDOrRplsrBfKx1Wn8AB0hMM"
    "2GNkaDJ3/SdegYhXebNa2XDqi830QjrtO8uLdpjKFf1szfNqTi0t3nEpgY99plF2XWnoMvHw2sT0yJYRrihW3kkMq/qdxbC+"
    "s3BMNWU4pnq6VPH1jjs/zMCo4t+wFqrcOgPCVniS1dG9prl2J/Ym9vS2nnD1PWjUWIGUcFunWKhV3zhBY1VndfAwGkbJbFSM"
    "ymTSNKyiYxo2iJSyWbfr07r5J0/QvFE+Zw9MSG0na+qCmkaS7fX7udgwgiieXDGXOKgI+X3Rm4WTQkG5uzgYKyN8nxmi6o8M"
    "0Y8M0Ts1SU4VPWYGiCGTHh6rG/an3tJ5nSjRVzI7Xi/7obp9gX9JPSgW96rSuDEFKb2jFGP0YRlW4b3Bu3zqEGeYWF8lr5EA"
    "PoHLrxjDDBcouqJEyNAMlVQRO3EFOCjPp6bD4GvH4mIPfrlXbgZpBc6nJmXw0g4fTUc7YRuqUcTb5+3Mx8SlYdpfIbLL73Sw"
    "W4/IRE/uUhYALuF9rCEztPQRmoEYah3dD7AwGbsaMo7CeRh8Rq2uXvE7TMHG6HL6R6rNYwwH9sdhZIBp5HZJUD6r3lgRt3Re"
    "L0UTt+fT2TknNHG2S1fz9NI1ose2jHySUcA/icwNXSBXcili2hjOmwiiyp9Fy0IsYnbZtUsK0s94QTwAaOFdSuy9kuqJuXQs"
    "T5pg78v4VLoUyJxiD0XAVpEfqIbUYv+4vttbmb7yJiYs2q6YImElXGtW/rQjGXAKMVAFVPOPGqwRRMkHRVVe2Hm858u6elTk"
    "Q6x+TW9Fif09KJaVfE3E3n57U/jPFvVslAq1svl2KcRGvVavmVbNqFSKJaNiO0Wjac9qRnUym0xrzUapWbFOm0JE9gGBQBVU"
    "jeUCRV47w355jo2T2bV4KZQyWYUuZaYtl+HTCPj7tInypXo5GQ03APtaXMVmCT+6DzEnMh90zAhlHd0DE4zpMosMADgE0po4"
    "KCZpT+pI0lTdkNniOdQgMSknBmGKVHv2kmRZY8Jc6tykMi6H39VfBj1i2fAlWIJ2D7jU/XBs/g+seHB2cw86lPFes/VO4q21"
    "H9nM1wwd1lKGDmuny2ZuNRrMluT+LHDnm1gIw5BqDyTt19Pvtfeg26kQSshglgvFWv1tM5gTq1mtW6WZUSo3bZAbJcuwbBsk"
    "aqM5q1VmtdrUrn3zGcxDlcgeh0+RNIyv0GP5wkKQJQw99qdIEP5QWN+FwjqVl2u2fkq0IF8nkAVqKctsZtEywNe8g2/gtlO4"
    "JvCRXcgPXBaq28OtQuPuMqlhqjTnSlaor5erewMSGIadQ2qdyMakwawN3SN6Y5vlvfRc3mK3lM1CJbny6vQxiLIFerlhV43a"
    "bEpnjFYMC/xBo9mwmlatWGlUJpMTxyBoFy8fQ2V8aqtwe8Mmch3YWHN1cymKlPUmqRhB4OMATlibXFBbDkekZP2cDjmiwZEU"
    "QRT8nIyhXGodlRPiIigXAnmgLescWei8BzkEoYDLd2JZ1X9YVq9pWdVTWlb100xfbatxw3j+DvP1iZRpO4ltSbwMeNOZEvV3"
    "oUAvt5YuNytvp0Ata+JMytOi0SjbplEpV2dGo2SWjYZVtMpmpWpXKqfpKxPzbKAlc3mi5KGyH3uMtv9SqpdzpDUY3Pa6o3PR"
    "/Ftppc+ThFm1IDNzncnx0P5Cv+sv0pxBr1IW6I/rWc97r8Y06HR57YTWEOAicy++SvOlhvoe34Bs6Mz5Gq/qsXbxYkOG6CJI"
    "BtyywBwE5bTP1groBftlLWi7rBUAYW3IF30JVBDMLnwse3V/RX7flIulCll5QIMrMVLgGUtVeH+8YIqcu6AZYtZEj28wMG0+"
    "0wEL9JvzF/qJYE4Rb10PkMnSOp+l5drkr8U8YNNaPTrEdn3awww7G3fvL0kGdpbBC2B0x4AOjgX6JZHa55P5+ves+baH7b9m"
    "2HGHHynYGjzVvu22+gXxpDFzv5yTGMiAAENerFP0STQY8lwGP1cWR4MucgeXn6fnqB/l5+/NBnpHPV/3q09Pu1q66vSUq8XV"
    "pu/REY8N9TBNtdTFl1UzctZ77phVYwojQe6zypvcgYfXFM/9fruD1b+Y5gXd255vonR/mDufnPnD6Oc27eMrlvmTmvL7TTPb"
    "IsrZzX+mCfWMOrdw1MJSYQaJMTa8UteTlVKiKLdAfnly54ENE1u8Sj9Nsty+QRpFBSJGYDzyert1/NxlMEbw6UxwtIw4EitG"
    "4DFMXiFbiG3aBBT5YJoPkvMeJOfhcFo8yBldw1tu6MiR0/V0Ok5kn6r9Zv24mZLkgujinppQ1yFLWdo7r1b6KOcF6G3rD6yI"
    "3AcPibH3WJYL3IZE818SpSDARN1w5vOKSjr8gr9XrjxIiY9iUqzyrdS5BxR5eK37RVx9+1cra48nPVntHhlX8UpEmbrK/QBr"
    "6WSGUnLVdSSmVD8vkWzQWDBH/UA52wUduvg6bHV615Y+hZEE126sbBWi5VML0WDqmCYT3+Lqz2ml6WGClJqKe4pARU3nTyCJ"
    "cVhTDFmW2YwjNZYSXz+OFge1W7nF+p2J4oGQr/f9cffXcZ6L36k6O8aIldNZFEN5gjIE/7c9zJFnx1r4kStKtuMvXRCRIa9C"
    "mZJFHzT63fEv98N/FGK7bH6rcv+Ul5zSemTxgvoUWuMUCuMVdUWZZHEEXIKuoI0J3TX+bmHwMV7wBO8Kh9zPqJE1Zrfk9EOF"
    "s2mfNweFD2dQQx+0wCkaBAMombNgL2RprZ8+Wy/BzAIZOmShvkKcksKPB2vE3Hs4qt6iHr1NdqS+CtSvusPDR3O8veJ6FXM/"
    "0DDCO4618I8y3WMbEL7WfaVdKkMcHEsKtqoIbsrngUP/4ENygX1Y7gGVzuPGtS3QNfkwOwk2wiAB9QL4J/3AaFJKmRxrRa0D"
    "wUcaD+WEuubDet5CjQT3m5DFcZbhUSz+YW/RvrccP9UV1nr0NhsTOPgjTQxh2ZcsAhHJliA1d05+BtagYkC4fvwZ1LQsLEWT"
    "YAonMsb4STHWduSVsjJLlCeYxdqevlJTVyAC9HRRIY0wTc8xoSAZA6JuKenBuXB/3jjmglO/SAqcWgum5QjLW6G8DXNZfBTt"
    "LTjn+LuCrxWW+z487/wrRbriCDCrUx678cstJDkVchu1F0ibESND3gtlcYWvJWGkuE0dejUh/C0T4m8TvD0RmW2VttV3X67S"
    "LDRrb9m3tmwVS7X6xCjPpnWjMmtMDMtqNI1y0zRnTsUqlmk29xQFn060bS0WSbbm4Er4BI5WoDefg59BX8msx2Yh37LmoNuC"
    "29Ir59Fa2XOcQAymMtODdFwkm5UWutyCHI2hDvWzGWlmhW9d49RacctzzINXbPb0QoxxY+0dGuekRaeat73n5RycLjunajd8"
    "h009R9eJKkY2oc12Dr+nuQfqZGFEcOp3UqDQ+HoFCt9ZkraRMknbOE295UeVYcG8sRjloZ8W1mYKK6YKWedP9r1jIuSN9zLl"
    "LyS4kvshFJpFE89LpV/wAVouhjCe1Z1m3SnVjaldrRqVSd0yJrNJ1XCmJvy/IkCk0USUhfRXxSzVK2WmvwAZFKtBFMzEH3nM"
    "S9oukoL5z2HO7XT7aJF8clYgglG6oTJyLN9bAG612wfZwF+lnkHgreWCtPz6aeUo+Xr/nGRLOT2HoUdx1OmrF3TACHfrArWl"
    "1liyZ8DhypZzZI/QuVK8pwZjsGHAZ180rkFlk7m7H163+mTQHXeH/C4CG8ZcLZvlYq1uKqp2V5lnEJUATelYKyU5BScwc/HD"
    "2/lel3oE/qxzf9fq9cn1x16n1W938/Tl541PfbZOd9j7udthwJVGrii/WIL2Ak91afEaCmpl52n0g76gRZGZNcDrQ/XoNgaA"
    "LHfhk7gKrQz+nCFZESs31GmYgZGfy5O4iix8uwtvt62lu2b7FAuFaSW0GnoXcVVZGfwZ59XMvQlgR1lYW0CveOXpZOkkn5PM"
    "L2gM4cxcvNfirWgCBB73ZXXL2TRYOt55D5LXorolVFyTl+FnRw8sW2oGhBdmdn/u9knvSscaMMY9mGfsCgwaY4wTdxfg5COR"
    "bLkV2kBj4kR2wCgHyI+SCih5hAquwstbA5MVn1NIvmUHdmmeEngI7gDuzj19a9S97bbHpEVQQJH2zX2v3SVX90MwfKkjORje"
    "/9wDqoczI7kmxAlFLXW4YPdqs8JKX0SnOH+4wwpF1dYUQ0bJMXBJISUnLGqtE8q4mcyJ84bj0SRlF7OqAcA+4A2piX7T26xC"
    "yQvESXgCc3AREZGEtpwSM1Wbv/ED8fjVjFlyH86LhWY5H3ha/80riqeqKR5mG+zSOyDVrnrDu25HUz6t29v7X0Ia6Ef5/o/y"
    "/e+tfH8b5wDrLP0HMBzBQQKIrlyHOQapoxiqFRgfxSgiX5arsddWnbrdbDQdcIDLYHk2zaIxAXPTmJSm5dqkOKlP7MbrRTEU"
    "O5GLR5xXHmTI6BVtmpShJC8fmOlKSvCxmDgFAh3NGIUlwavYPNMUk9C2Gv/iohhRKdaKtUqtWq2AnSCnWLFkYub/IwSf+wsh"
    "wVM5/iX22ZDqxEWzfEB6LgifBG+LG6q0ySE8zSey0gTn1JqHa10zMbCRElrAJREmmRj9qwqHKdO4gB5Yybhrjds3pPtruztA"
    "N161f3ozFbD4WUAiHI3+NiSHGAycwmMBc3pMJYD7yGBCzugrKHL5tHpe3ktBwXbApCeWEX+ZOksmlwRNsOkomS1BoDDd6zd0"
    "01O+DAKNb8DUms2tRzn8FcxaHS3UAk+Hkm2BJNhx6bxa/KYDSdEAUio/Nk0AibuhkchRyjaaJEs5VbLagfcbOKFeyHW+owsG"
    "1CYqnoOMdgz6767gIJ/8jdyDjbWC9Qn2q/BTxreKcfGt7z+KkxZCsaEds1pomPu1ujxKX9sNs9KoTspGcVaegNSypiC1JkXD"
    "LjWrtjmpVhp28XWzDt0hYQ19HT/DY/s8dSAUkrv45M0/OaT77IDdtpiCi+6BkuQ92H3migzmFise5IvRte8X4Jc/Lze+cb9Z"
    "Y3gCTcwbD+sC8UJHdjBqd5Ahy+VcPrAZ7c0K9aTN07zNYsOskezSfwE3AC1Aa/nCko7NZrlkkqw3m8EHz7zgE59A+KxzgbFu"
    "u9bjwvP5gmg4gCX85Fhz6tPRolJQ7db/gDnZcdCX9104b8cF45V6763OTSdPBhbYwvK3Oe4dM8dK5jgAqgAER4JqpYKKzspl"
    "Lym2LUvS/ESSE0Mgkv3ntGiy2NM6enQIXJnl0tYDgyHzCH5Wdwm/scHrwf6wQE0OwPSqWYR3ESLkBoT7ClkI3sUIH4f4VaVU"
    "BNzGQGsbfgcqfpnH1P3bnTxGnlSqoKUXGubvGebvw5jHgo2FEsnBzzCwGALdAUAABvxv/hLUFqyow06iWAAWngIn+BkdGHo2"
    "sRY8lb1qVApfcjIepN+7+inWacdJlRgLkegl2S6yM4DKW3zazClqxpzuCwQfxeTbyxKNSvC6uK9LIdYejClkEaj1okYnGP0M"
    "gz8W9JZtG7C5PcAtQqYIZo1Nz3QMIpXC9sMUuTfnwe5pG1gPoyQuA4JD1qCd1gIQ8Bk2EJwmHKdr9LMlF3KIRbar75AFvphc"
    "UvGjMyd3mJPYN+wR8bJ/sP7vri/JnYNCA9xiLouWc2vBI16ixQ11nYGQJ5iNwdQuCgT23opG5PIZKeao1j3DRYiiEgHA7VF7"
    "RO/v5sn18Hr40G/ddYHJr0fX9J85GlGUtc2Z9n2/DyfutVvDLrnrdug/8jwGRmOxl+3WmC6IAXvxBNvIoNMZPYClNaQP4d+r"
    "5XomUVgqIEmESIhtwrc79od9z7aePP6t+ZHAD1qu0VsK+EQS/KiGC2GHvdDuyedEMNQkN3f35K+VIgFv7q/lolk7a3XuSG9A"
    "2r8pOFcju3NvSjkAlEvbWyzQJABrHtA/TlZWnwE2vud8oq6jDqZkKJ0cIf2bwbCXhAj6kO97Uzfg71gQk6xC3QrNS0JPQ+cZ"
    "oarCiMoObjtsxdxBSAtwtpOsUrELg1m277iPTxNv9eR5NrlhEBzg3uEDwyfUBT0ffrRzp8AgtUaeJ5s5Bg90O4SbH74uUumZ"
    "owaaqheAPrnWEgpJqhwQTGtyCWb7o4OegzQdafBeN+OOUCfSNGGMtLa+eAvv+UUxUcp1c1jE//zKlSVQ4gqWGDo+C+/APsao"
    "fmgMFaxzNtqEVfoXG5VBsaG/+7LVerQEhAMY0TtSwe/DijzbKv7666/4uOv4ubz2sDR5xMGYlRb7yFJY8fzDYTpRkL8XzYCT"
    "0bm7aQuKEenStcfmddMKo0XYwThDHForBlTY0521sB4dSeRtxIyMI+nwFIGGqJ0dkT2wNfZXj0tA3KjIMElRoEElnW9zPyLU"
    "r6EtKRdhl4D93czFmecT58n65HqrgLqzz5Sm7YCm7RBNLylN29JEolJSZ8UI0VBuy3NWYxsNdqn7D3EiS5VYUZ3Df0AsCSAA"
    "wOYusO7CtSiUgeweN5LwIkJJUk2E1FiKIWRXxxIe26M0K0IiC4tatVOw39N8xYM3exCnPQ+JZ8WMOk9SI/mQpXSuGEqKdjrf"
    "roa26gASOp02MOFw7RwG2jlpty8D5lVgpyI5AKQCxO1aTgFjst0UASRbRwFTby0nW0UgQnf+anC5sp7dOdNx7Sd3bq+cBbn6"
    "5037bBgCVxpbMqMzjYQn557Qx9lp0hieGZ4kcRbOzF0TNH0evdWLDkYZK9mP4LYB4DioB9CBzwwdWOuZbmYbB+9BeJL/jiG8"
    "lPzpYmmuPEB0qgnGyPMR/DCMUINVXJEDjbn0XFAza08eWiUOThWq46pI01honoILWsO7mxGjgfFo9CZ0v9OVZ0ZljIJjmwVd"
    "insNKbkdVCoqKTCSEoJtC9B0j5/21VJtC5E5fTIAiWe2M7OwyabyjQU8IBIlNAMtDFFZq5IVRRjF82JMjwS8dy2KqjE5JZNY"
    "SnbNtYOe2CJBCeRK4URkwCt4IajtY6PV6Ly1RdCUk5GVeuDgkCS+UYO+0W2TfBZYm0UhcWgWcI94uswCDoUJqoTWlaj4957W"
    "41W6pVdM7+2oD4+m5G5BFPTWzrPPf/Hv4n8KNBlAm8xtNa1jby3GrdcbsOQCFQLp3iopb6HMSP2tIJER+wr9oSOixL3FzMOX"
    "2rd3+BJ7E0Pyad8thd9tFlO/Ww6/WykV9wMp6wM4+q19cz++6Q5bg9/IL2eD1rjX7Y/hX+jaj4Y/t0cYQb7r9feDPMP/1VWv"
    "3T27/zgWy3ZH49blbW900+2Q2/tfyF3nDhxrvvyh162kYr8QSv3AxYQtcRFrRxy2qLSMT7mojGBd7Bu8OvSGYavzAKgbwynG"
    "KXoGjFu/3vfv7357aAMryZjLjhePEBuHbHQ3YpiBF/YhL1SbZ9/Gj3/68gdFiaUpbSidbghN3wNjIDFARrJsMq3zBbOChAd1"
    "0L4ECyIScMHKKWqONkoG/m81F9ymU6KjmiVLL8Ey9YLGR1n8iCSP5QA7iJ7rswckxxI8L7Sh+vsy/p7qu9D9IN0KOqRhwRZl"
    "uZ9ypcfdU/Xvq/STb9yWjhrR3fcSss9KxjgmCx0mH1bBqKWUUY6I0CEigFsR3CCguh1OJBd6EEUtkgwY2v8TvhemFxwciPsY"
    "6+P/6NZHOgT9n5QIOnzKUN9xqT+kQxy5kubRSbZLU2gyAxx1N9H7BcuHB1Ej6XuFl49jSCUPfUKOfBV+OXy69TgUQFmxbByP"
    "ByhZYtrYRVhfzA+J1chSZ295RjcJsdhL/C2Eg6Q09UlaugQb2N6cRR46na2yHVONYyQbzRJLwabGqqi8+kkJhykJAoPdjYkP"
    "INNkQEq0SZMWn9nfqA0MP3x/HOnGlJQCPxbZwdG226XydCnN1+2Irp+AJZNRxvhz37T0CVn4KFoItwQMVyS8EcZTcnZw1K0I"
    "rx1ls0TSw9R8yCuJZLzuJpRivHmyn7rj/V4UPxBt3sATDNspYoffgC7cy7vdjrXqEVhbazltnoWmjFk2U+ZhMeyM9TF4gwo7"
    "g+Kv8N+xQfrjvJMd4pcngL9Z32NvAXx4I+FdApjiOzZjcrT9wxvdKrEMWgqvRDPCFlJkA29mGKlSNrLrLai5PsJ7OBw1LD1d"
    "oHcSkiauh/Nnha+OT14P8FYKMYz9fdBaLDbfAq2HJMZPgMb4QOO+yNy21VQ4TtjGcWHvU8T1t1JGqXSMbSQLzLAuTSCdl0Cy"
    "erWVlvSntySwu0XYPkpj+56clfUigK/luO7FyKXyV5DP20I+SjnBGdYSFL4ZhtbrMY5j4ZOr5a04Lsa1kE2BX2DDxfyFV04E"
    "tQyRGgp+u1fUSWwZO8HLNc521TYUoi21RGEEyQYvW7M1DS9q+5OXR3LHdN9CuJnvofsWbQD8ooAn8ZJmpVkt1Jtvd0mzbk+K"
    "5akzMUy7Roc/O0azZk/pJXPHhj/O7PrrXtL8eFmsYPNzWlfOS6u0QjgkZKCOZ3e9Zi1nQNjdjYxStVgkWUDbDMuG8TM52jJc"
    "mC103ay78NfuesP2cTaT5ev4ZIF0uapjvb7GsX1ReNMb5aP4rsVb2DAiBkv2D4dkGmadtG9bvTts93MHHCmjNKMMljFTH/EM"
    "trRZYY9i9iq/7YbaNjjWxPsCgrlkmOjYRnpA0C/LWv5yo9xnMQRWVxczbgTUN9s0rSbHBmR4VtrNBf6LPadY6VqGZHlA8Gyg"
    "AZZKXP746OOl+gprBzbW+qYE6PoEfjh4/SD/fi2Xy60SyWIxFABqQFTUEYUbuCsv3zJbZZKFN3AIoIJM7KRWycUXsdMUh3IF"
    "N0+2euus5YW+IXYYiiMxEi62Hwe8GXOLI6/UgyHkWSMtZfmswPUZwiKnEu0ZPavOEcn3KyP3jW7aVy0F88EeChly63m0az3v"
    "YBLXBIjeKeYtSOhlTHeNVYhbaVu0SfZBvE/XlLgtlWPO6J4oHLJxQE7gJ3p/YqURcj4e136eBBdtPK0FEpVFebUTksYUYrCk"
    "aeaJs54WcuHqVd70iLNJbjuf5ENtu3DCk76IYJ6c5B6t5pOaYsqK6sWiYOPB9SI9UyfrQkXOjv2GXTcKZjrEUzoViOOETl3w"
    "UcThmZRQcQRGQN4/sct2C0rLBkhgjWfZVw4tgNxDV8kCyCsP+zAF3ERb8wLOl97cnb68k/rH8rdU/6gP+OF8c3G3w+AOscfu"
    "57udh4+j7vCh0xq3zAtVxexZgBR0htT1+37LXGwVkEfMrFUBeMQaGmC/s8qocsrKqPLpJs+2A/l/xyQgNVkUK0XetfXjpKlU"
    "71yzxxi5YiYYRzBa9/RXGmFjB3CdtMMIpe+FHLX4rx0XCFG3uhcr78XIyT50OZpi2MOHjumcG9KCWU2/5YRRwm1aeuNQqDyD"
    "znMmjKqFxSJM9lKelOp5Uq7kiQn/pgZ8YJsESjhqoqDOjrduaKfreZJSjjf2ksgrhnqkoOOy7OEJBAIy1IctMi1Mc4k7OfUI"
    "p/wrkelWynsXPcDvl85C82xlw+nkcES9UDIbbxeOcBywV2alqmE162UDliobDdN0jHqpDiZevTazG68cjjAvWWNpNo4NUOMs"
    "6CUmhiA2vcK85Hb7NvQSpBbwO8Afwn7E6l8u8DMZ3lFZuU8mncrs9XBAXfpyrVnPFZJ9P3FNUnwRm8WIE6zFBiOnYLLGXWsz"
    "rtFlMy9xMjrKOjCKfdG8MsPPrGwB/RSlzTk89cJvvvNN4Hs+3t1UO7DS3Dv4R5+xEfG225FmKwUSWrFX7unXgiadKngAFysX"
    "RLcdwIdSDunKDwzoB/geOJI5HlEIsRcz29Ce4epFfpzjnNFWhpkDcnGlA/XWG3rwsxFMOTmPTDzRnk8ALIU+7KLcAvXU2w1g"
    "9lxSXwOaIVLrebXGkbzv78pdg9Nt4b3VP5z5i+LRwzK8USrYWLRRlew2TLfIYgoZ+P4uZAF7hMIaO6WqitI88oHkUnkO8STy"
    "aKYzJH9v9bsgLLu8U1DQUQNNxMzlDdsv/PpxZT37+BsS3PHhh4HfDVrDca91S27uR4PeuHXb+1cLfSsA8o33GTvV5LWLys4X"
    "RDNskEIEt4IAkZA6C0AYHIG3hVCO6KcQVrFyKkMFnaggVvlAOk2SFihlaNtMxG+B/AIrept1kGqU/MqpM5OQk9gl2WgfjHx4"
    "nGIwIE6Tdnx84kGxiD0UlYxFYF4PocR6gBfeSfCh+PWCD9sOKsxG5VcXdCbXzuOLN2nrQvFH7AF4cXnz/TnPxZTOs3ncRKA9"
    "UcEGpIVciO2mV+op7nvsItHmN6NF/vtMGlaMDhetKqb1zdYO5kwNqFZ6QG0bBHgqSB1+fSgBUiVQAq0+rcG1Jj7z+k4BOGXd"
    "vsdn+9zRj39T8AyV7/QWbI4cXrdJWcMTby+IQmZuIiSo0KjloOli63niPm68DRtREqNaC4ci6xjZDCuAdA6he7elexhuE3aV"
    "7tzbUV9+D4GGW9qsUR2iRJ2CrGmwHi+8QuVvpCNHB003qxWNxnNXWOHNxOBErVJolstvF5woliuNabNZMRzLsYxKw64ZjVKx"
    "aUyrswqYNbWm3ZicKDjB+7HsmEARO4pMd5d8OQHLd8Jx68wAe7nRLsf4F2d1FjNuVk2IwnbUgWZs2gQ6ttd0Ib4Zijoe94ZP"
    "bOnbE0z2SRqqNojp4qO0q1GmeNIGoXQiu3qG4GzB7DDROjc0yEovX4yOpSbZmDx/bnc/0M+07GrxyAfK3abcJ/aBo3yCz7KZ"
    "E2dY4EXRG1OlQhOwVHJFKK7DfVlBAVRua9PR+F/y1O/H8pBkytpNVUgMjAaU4SZBl8I8iQzFiyEe1mhIIJ2FU+BbHts6OEKw"
    "Nh9IBlrHX/Pf5hPqGnhQRu81QLKRag4sBSiVlDw+P24IXHQxMRvkQh2Ukhd6UG1eBEoSNSS6b5JUIzNh/FCvTi1WQYcRWawf"
    "1MIOvHVLOORZJUaRJyB2tAgFHEgZVLuL8mhzJN5wl8cDldZJNCgoGVCLAxrwKnmysFE7F+CcdWiEgpsL4vi7doGVQ7SikTPB"
    "wa76HmJbuurXHiKKBZxYLpAnEEB8B0yE89YokqkNpg1QUQ5e+H3BlutQFa6RJUcmDUsGTEensaw41wcCXcnu/L4YirfwL0p5"
    "ly+7/vPRxpqiEMC1PgFtYfhQMY6U+LPgajnlDRcB6wHxBL9I2CjrkR0nOei8KX/psfF09PqHteazHTdzS67gsSUT1xEzCIW1"
    "wMYtc4DaAJWBs6KCEB8yWUc5nweimJjAFRxr+iQhAUgbhFlOHB/+xiBTQFkTWo9m42w+lmhGy0zwcLQRK3/QwOF+7AdlpGS3"
    "189q583FIyG8G95XjH2dRiF1qKmaoa3U9BgEx/+Fd4GTQQ/8rHxVk7PiYzhrNfwx0cUxENceK2NGdMgXCqEXDtyeRvDBQpHN"
    "5gmfD8V2hIqaboMjJ4/nzLGpGqKEeoyxdtzzUltf6umFO/0Df8N9o7nnO3KUFIo+OlaL+UqUdngcku4diE2BhEGU/8jTWLa9"
    "4n0GFYUYD8veLLTL0BSuNZ5lvXoJQvGRz5wCAdpaURxcqcAUz0rWGjrPwN8jTFzk2b/bKAsQuOzHf7lLkr2i+KsQ2310134O"
    "YNf3PtP2g9jogma5OO8KUK9YUb1EOf+9YEtcnyJfyj9r4n1S8MytgwUbsIXn5XkPK+ZTZe0X1hQfxBkkL4EwCnShL1ZSlUmy"
    "XIKz9lhbghd+UhzOme3JuZw5xb5d4RULMbM3+ndN2MujykCAgRT1DP92l3N5XhZPkG/qR81TEMk0o0LxYpP98Cby8A2KGpxx"
    "Ev8NodM0OCd/KnZOsfhcgdMKMOj0D12TR4dgqtwQaz4X2nQZd8Y3QvOflCNwEgHaRrKFRAzMAg3DjoR7ExkD9LHhrz9SBqdJ"
    "Gezb6S0YtHp9xN09ZR3eaFBzek7X+GyfskXdIzo4BNcHUr9QXJAjwBSazY0/HrVaW1+tfdxq2uRv/PHPmxHaJyECTrJO7Sw8"
    "NKC/D5w/OomXR1DE9KEU3ScYU+F3whHg5DgGiwcfGxw7tqxS3X5iXLhIkXZI6WMc3JWCEOG4fy0QB5GiNMm91GDMx1aBilAC"
    "VmGCbnccOQeVDnajHPRT1OZS6iq2xSsQhHS1B/gQBh0UkcomzT5wUR36Y5irBbOqwjQE8dt0AZQDAohvWqsp4LGnztnOJ6Uf"
    "+ZPU+ZNKsdA0S0n5EyWLe8IcSs1s1mpVyzbKtWbZqBTNmtEsmU2jVizVmpVmc2ZNyifJoexZMfcNPX5onHMP0AYlSRSQ1B9T"
    "xAkXIQXy+0KrWWq8Fwek8j3WLH0dr+XU5nicAf3qxj2H63Hg2r3IfgBLv146kKVeLw5o35kPU0npw1RO1yw7sVTprNU/06uJ"
    "fmLJMTfoecWEvWx8Ja5kvW5NUzakZnKvVMmUT2rXJz8GFvcU07MsVBoPG3tD4aMW7ItmJpHWpbq6khb4zsMlmo6Vo3ost7nR"
    "9ZieRGg7FqpUK1FYnIgyaOu1iTP3Fo++1OEVkk2kmNzblrpVolf6Xocf3wLYoe1s4Uq+EbED1pDkLVmztSAxsQySvbsfXuOe"
    "B91xd0gbFMzxuqEd9EgQN4Ki7iPsHx55ME0hqNlibK0I/8Z83ZWXI+WttNd2T+mdTbyhmQSoQUw2X9Y7hcqcsNWMOFWOElxE"
    "Qgnak0WY3K0xmFLAdDhhIxcmjuYO7IjAxIGTfzHuc3o9QzauZIGVduVSl1wfbJomQb7NykrF12kWBU7AWYfiD8dp0ZFfSYxN"
    "shF5jBVJmxXeZnmm5WgiyUNLJlDWPIG3I+InzhegfrVuBtUOk1QUv0t0RO8HcjeP4Ipiw948AYv/hUyfPNqjg+dHeZWsTx2u"
    "mOZaWClAZ+pi4pi/irnnsGv4GgiJsVtPZuNvF//mVxf/r2KTfUfKgItaVtLGpTxOAVSkF60eyKgCPRMUgpSa1bJZLtbqplZj"
    "Zmk6pjeS0wz9uP5UvKaTCT9ruoZ3MD2O1+v1q1yFLbKPo/attchRygMI0wpF6BVlQSX3bvkf//52sQ94WDorkiz8DxQ1e3na"
    "ib7DbrGcRXGVJyhr8H/bw1wwilG1KUBmF8gA4/neswXA2EtI0wINlPpiRdTVjrWau7CgeDULkvusjEPoOfnScZQx4j2Psp1V"
    "lmKZzqmF/EHhiRNGJk4YlNiuMyrvIcSfqBISo/X1aqHRqL7hbQfbmRWdytSwK7W6UanYpmHNGqbx/7P3JryJJNmi8F8JzYwG"
    "kEjMkmwu+UkYsM2MWQao6u57+6qUQGLzGoM/EleV9en+93dO7JEbyWJX2VWj1r1lSCIjzjlx9sXO14v5QnViF2r516l2CGty"
    "oBc4vW4vBjZn1r8rs7xf21xWzZeNaoPOpyY3ZuzoKPtYHrs2LhdTjXmJu2j6WjovEWeJ1P6M0UoWCJqFlFLr+1QAlv1h3a3v"
    "9IdnWicnGRmB4DVa5g14DzGG8huLMbwxD3A5oQe4fMIslnCTo9igWZ6dl7EdtNVf0TVXfg8yNpS1hAvXQiFXyhdfNxTulOdV"
    "sIBrVqFUs4EbTOqWk3dL1qwymRZL1Vl1Wi395KHwV4qc74EJKbdkrq/KtSZpsBUyob5VkdS9YR45lR3156oz94ffVRmOHDNP"
    "05vfZyy+/CsW/ytC/f4i1KfST453iQoNQrlBy68VBROv5n7O8nf0c0bHo6lptsMvbAZ3Xz4eXT4qHr0HIZgm6XuighjIFl8K"
    "styPHU5RuXd/x/wu74Unks6x50LEPYvL+9jp+jY95qq6impYgx/Suc2jEjzUwlsJaLGVsNAKNgAOD6hojQOjqC42YhKIljS/"
    "U7SEB1ObLJgqQ7NWaAwg6P3nkxNcbBYVFQmweu3xb/3hvwlYeI+LLR+A0hv/9u/PnV7rop8jPOwsvWlYPRgAYC9QGahKGvRq"
    "Q1Fh+cufH8+NSy/HjUOF3IvzYb+Nit5VtpPvyo1FAPLaCD/GpZ6geSs1JNZrh3b3erdMORAFNxhz2hcTz4hC8UScWlRWa+Fo"
    "oS48TSyjxVqvP44Zw2YycHz2R2Hi4Tp2hNxPf92sgUVrcj8jeujq0Vsapo7L0zldqPUAHd1+DR39u+Ss/HBK+8/mkq5WctXq"
    "K8Z7a9VKtVJyKpZt5wuWPXPzVn02r1jlyXwyrdRrhbrtnDbei2QmkvoqvOOXNnGMfX7OGr1LLXdp9voOhki9rBYhtQl6gyfL"
    "hXevh0Z9kVvkoaUG8PdtimYJ8jl6cpWy1jfa/3Pg79g1P7bjelrrUyejwRmeRMXHybCW8NeLL+5K9HaR7fA1SOjhZG1VfUdw"
    "EkoXnh6rnkalqJO0OtvkeXcUPRMIaCP0QgPaEmcb18LbKVqiIZzOgz3vl9gsniZ+RQNM0IhD7tyVu1lMQ6eYCvVfVhuI5UCf"
    "+cqalomhebhwjlzyloAC6JQEDbhrOv5WN/G96fqR9doyUZAI9llzrTgmf3BMfo9rLWMbtFHxe4gtVH7F4F/Sx11J6OOunHgw"
    "V3SpDb14TJ4YlxN44qPW147e71PZgaNYXvy6ilLlPShJFGgRcfpSrlosvW6cfuLUy1WnMLcKxfrMsp2CYzmzGfDSWn1eseeV"
    "ynRW+eHj9IeKjz0OnyA0zvTe8Kh4TsXCfY/9FGHwX6LqTYiqPSz8ITdxDCmkCwcMisToht46olT2VIXD2DWZekawt5+8dvxS"
    "ppXya/hoXlmeHRUHHQaMzLTvNPE4ALi8CORVjRdtuE25tYYNf8dz2AbGFXT0vD4Wiu9csyiWcsV89fXcLUUHJGdtVrYq8ykd"
    "Zm1bDthqVr3m1J1K3q7ZkxMPE2BdLj0kNz6mWzijNeWVzaY6Z5MARYvKHU6WOPcIz4vfNbcrFz0LMMoXIIx8bADNDmZ4KNnj"
    "dBqivh9m70fm8PumIYZ4WzS3xiMHIo1b9nvj9u/jLOmAysO4vYoibrW+7nA+oRMx6tIHzmPIISxFK4V/p0hahB4tvTeUconj"
    "qNmQhCz8dRt+3XRwZgEFrFjI37zNtxq6wcJSslL4N5axLdcTZ0m0hY0FaK9WHpRlVVgCXFMR5mWgwSfY3Fv423Pjg7+IeM+A"
    "qS+8y3RNl3nS1k93975Ab6qfinLYLR7Qj7XLbycDevSicG9hFEmZVEQ9ZmHeskMV9D34iPLvXL4Tpbn69nNHf+Vs/jhGQjWh"
    "kVA9blae9GVJYWMKqazGXITYeZkgH7r/Q3ncqw7bq74L1fIy2mll5+rV+uuplo4zcSfFad6qFWclyy6W51atUCpaNSfvFEt2"
    "eWbbp+mxGPIso1ywszCKVsbJMcymSX/sMAXy74VqMUMag8Ftpz06lzOWVNE7T2ZI6/nuqetURs68NzJtATgbI9OLZ1kEa//V"
    "TJ9ga/cFz9wK6An8rSvs5mLpv+MbkKNA+EXGcnva7VVuyBItTA39h960r84G6GXrsUPRGSnTrSV/6EmgguRZwMvSV/0r8udT"
    "MV+wyWYNNLgxpmuLYs7JM1ftFyvaXCFDuypr+jkDCh0pxN65fKavYBFArlghtQNk0oh38ugsZuQf+Sxg09ncuWS28BwceIIz"
    "Mdr9S5KCnaVwFjDdMaBDz3UReUZ8rHGvz8a2rLHH7hy7T/IjackxoAXfthu9nHjSmi++nZMQyAADw7tYpeiTaLDkuSx+rvR0"
    "CVpj5uACoeQ36leB0HtT8n713Uym7O2RQAav+VYqXRjZdWYe3SknEewzD4Fu7DzBxvYEm5lp/POqxnuEeschMxq1qYyGgnDN"
    "x1SGKwRcGUgP0CNDuuvNHciCkHlIYd3fKVuWnSP0MXsZmS/OBQ/K8ShOLZvyMB2A9TgK2W5Yem2yJvEanfNFJfJ97fj8FB1f"
    "zeQ/Au2/4yV3QB/eG+4YFrDftY+xTYIBiCNmL6h5lkasRdPiXiy7Wg6NMmcXfb8Wf5d6AYxGbyHtwYw0bNlp0kfV2V1k/UPX"
    "vUiHqO4M5Wrj2VT5V0M9oheU7URUtcBBwkoygBUzsR1W+oFfUg0hrMSEfjncXRUTTnAykS4wqewtNjc8lQ4VXcMhIy8i3lI9"
    "L1DbTSXawx8pKcfcWUoYVFpI0cwFiQ4E+0OPCXASyzp9lZZHjgwKH13zk/DLTqzbAxUCX6PemCa974p1NmNYJ0laPsiCSv7a"
    "ExpDAhM+UCjI3Q2cGn31Oj8qw91dnHg6Pe5Nc10WHt63EIkFM3k3SMFQhXdd+B79Di4anddaUcZ1ouTmhTIVjm8hvGfJUzVY"
    "rtlZwRamS1jhixvD131btZVneOFTho9m0RQGCfrC+tlWjibj/MaTcZKWkOX+XKnD4Khv/SxxO8kdjQk7zC6hsWtqSfDMZBZG"
    "4iqB8sifk08gQiiYRSNr/gyyCWZiU9+3ppqzjX/wpXvEuJPT0jmcJei8jvda6x7r3trnJc4liqhxLBUsm02x19Qi1oQAN4wH"
    "DDNwFUPHoQlfaf9hGvFRmhfL0hVLGt8VY74rxXxn+77z3RQthmLJTDFOnECYjEIVBR7YT5Zm+DDXq8jw2dGcNzw3gyd7EOv/"
    "BLvwNpbeOhvoOCyvf/qaN9NXqMmRy/X2XuHFoyg5p08hEvHovl+Euk9eQxxHEZXhNVHxoZ9b7maP15Fj+GL5vceT8+VcrVB8"
    "xVbARSdfqFQnVnE+rVr2vDaxHKdWt4r1Umnu2k6+aNdPlKsYkQTYWH51nj0CR2NSWv0NkkVWBT6t5K9oHaHKoN24d85mtsTh"
    "8qA0MIlFfYbUa+yvnEAvMzAr47UpHsQ0Cwp9FY9Um4P/WADunDRQhIGB8vC4dLcogrQO+dQNTL9/dIDJ3VP5iZrUjOkGh3Xg"
    "TY4pGahUh3wnAcPa9wsYvrGoTi1hVKd2moSnj/r9xE4ZjPIiHVPa9UvkiMqe9qXHOL9q72UWrY+FRQqlei5fr+N5QX+EPckX"
    "UDsXYTwpzGrOzC5YtSmwFLuQL1n1abVmOW6hPq3bE6cyzyPe/IKrUqnUK0xwATIoag3F+v//m9CchRolaZn/7b/DrXYPg0tf"
    "3A0wY+RzKIVcx1tjPrmRMZ9Wpp8v6TejN6jfuFopu3dO0oWMbxZHpCFrEU2twWb011KgdUZ6tV+6mCF7+OEslXEflgpldB9L"
    "/avfa4/IaNwe3DR6nbY+3aVQsEtVu1yoa7valYMFmy1lDG8Ttxky8cnzVJ9v9buNTo9cf+y0Gr1mO0tdHl9YftbDk0e9Iq32"
    "sPOp3WJHjMsVz1LXHH3McOExsc8ztUznATodncXqXeXhm7lnHNrnJEV9HeuVi2lHOBArkIC/M/K0UCafCG37QvbZiDoIjFoE"
    "cvbbn9o90rkykQW3og/q1xgTyOZ0bNeu3P+ssBAZnk1nbaC/km/wgiDMwbDTHxJk5+QKdtHpXY+yRhAmreImGer+EMMYuo1x"
    "86bdyqreI0zf481HtHCLfsUjShLcb6ibLtCpIjlOSjipdrv7slrTjC+L9ZIGjFStgrYXPAHnpDkFbKbehunk3HHD/TJzpgj9"
    "7Tyfq1ezyi75X93VUTa4NROou5g1UOtVZ9httwyO3bi97f/mY9u/slF/ZaO+tWzUuJsDV+fR+wzaFtgXANHNwmV6dXKbX1Od"
    "wm3++rldPy9XQ+sT3eqsXqu7YD8WwWKql/LWBLRWa1KYFiuT/KQ6mdVezubXlCvONcegFKhULtl4SOVu0QfmpmwQ91gMQcuR"
    "K59WBKr40wPtGSpkl3F/cVH0P+TtUqlg5+slELZysBoLQhTLQHBtUEkat2Tc+J10Wn9P0ctNXQjysUKBpGC1vxO1VIZvh+3N"
    "J9bwzeluc9z73GllfHug/Bm7V39dE2bwUMbPIidTZylIHBfxmDgKLb8DkMMTFn2AtH9vtgdo2coZaaAhdIx5r7gnQAwcjH7q"
    "4y1sy27uLoc1mYzNw9vZEcgZF6qDIRk73wBKmaxvi5wjLnDE29R9ZLzGbDDF76bnSnf5BRUbunoQbCxlxmtiXCv+62AW2iW/"
    "ENK1Mr4BfWa+dO5kS2NQGU1Kpjot+4pTcCT1xrln6uel2nmp/kO7Z4JumUQ2YRK3DDfpAv6YJGkGG+crSZtXM3NoLRyn9gu5"
    "0hvK9KXKUv4cmLdr0X+3xTX0yD9JH5SvDaxPsKmCl9BvlA/zG719n0hSCIU7Suxcqbhno4FjBPmsVrJr5UnRys+LE+BbzhT4"
    "1iRvzQr18qw0Kdu1Wf4VBHmq3RzDKpjb6XogRdu4Hih269WXpyUmD1AO7jw+g1TER7ER5PMjSjPg+1wLp9pvczAmCDlSz9eq"
    "ee5zV+XzVFmcPW1c+VDJBvXdewbVf8veQOwy8OPV09b1uEYNZISh8A2pl0l6C0e9d53l9p4nlbOMVKouPhrrIBwA6bCzLwuH"
    "qN8x+xNOwX4/Wzh3q7XHQgNXNjaaIOnG7P8C56eqcmsBui0C+ePKewTGhmIqI+0D8XaNBKk6MBj90bzpg4QZNgZ/kN/OBo1x"
    "BwwUPFy30/s4bo9S3I5jMlDGHqg2LGG2WH1ZL78AghDo/F1e7gNJpJaxI4HBkOrMnPs16br4iiVhYCCPS2fFRLMaqwgfBY7S"
    "vb4k150muen2yT8KeQIayj+K1TIZtIek0ep2xlnyn4+N4bg9vP2DdAZk8AcQkEO6jgfX6x6E3HbrWRParUIsg+/hGgYz6egQ"
    "Y3zZ7//qda+t62H/44B0eqOPQ3QzkWa/2+2MRqj+NEb9VNDHAFLYeJ8y4B5cZHbEmc02GF8KeTYSD18BjN4aHWIBiEYDlMEz"
    "WsHZG3cIfvYisKuiUUdtL9BPJugD5ydNNdcPD+5migYhGD3ZlIRKANHpQas1+txqj5png1v+r8yB6M8arUC1PejoZ7fQUUdT"
    "0Ht0nhGrK0z7YZgwDXVhFTAdnntJ+4Pxxy4llPaw2Wnccjdps3OGnyEtgapPzQdpysdfQLr5UMDvjcLezWDY8eHu1uyUq4Ke"
    "jLi1hynNqrQr59ArGYZh/Q7ucf0EGQ1agxaaDzQIfPtb4zJfKKV2g3VFGETSPXdxdz9Zb+7X6xm5YUce4JGBkIf3KCE6HvwJ"
    "/DZwxShkwO5wnY3Hp87u5jlgswHkLuhz5Bp71AJVXm5gE85D5tCrSw8ParP3oCSoIfhkKRPn5sRhT0t+HiqHkoihrPLAUKEK"
    "v9wlUhXyJnD+Ofbncbbu3XpDk31SoFxYZNQAcrl0750vizVCiGMGvniagC6E7skG8Bk3E4lqwB87JHP8o1eTHzarHZSqWMwo"
    "zZKrmp37lqEzl/lP+TmnZAt6J4MCHPVu4zx4xBCTcZiBG5EQK21gU2BSTp/JcL32YweokqUuj5S6kRq3b9s37cbt+AZM5/6n"
    "TqvdgltDuKxPjchNv4shhUF/xPSdQj6AMvq5wtvjYapQNlYZ6fduO702+dQZdcZwBW4y2hU+hOzi0O5KKG50KKLUdVY42f4R"
    "3XjUFZQQfa3uTVOgTrvwYESiYF4jO/Gh7oy0XByaQU8Ar+o6K+fOlRymie6YNC6bUc4MLVFFabYgvZzlAuTQauEocty4d09L"
    "FCDP6ucGaUmtLZwjqeHVl2tvCwcVekQT9gsfN9cbsJMoRPG6hq7B1JpsQA+lUksRhk8l9kAxoNIe1XRKcRmRu+wonM8EznmD"
    "vZUfqV/Aut/G3n3ANMec5i6iXiIabPHn5WjOXENhQE/tRPEhfibteALefu2P6RYKebCbM7obb+qunM1iHcvMHyaAX+ByJsMQ"
    "nR7hIOIB/QSRV3pwzJV+FHzH25vvvDhPiGf/EkYmI1WfwyX7ywK9A98626wfrfV8TpZrvgRL4vBpfqgRUmeq+/C4fWYLbp1v"
    "69X64ZmBvVgr9ug8+N9xV0jbQsw3pk9Yfoe/vlljLR56YilCxY4wr3lLo4HOZP20DbXDNNTT8MZuYyzFbrdUJc9Js3mpOFo0"
    "4bNvaFDl83r+WSj25361PpWR0V2myCXT4hj1BQyBQ3TLxIq0pu1v1rMnFAQbPDgCBJiSu+E6Lh7waYWh9qAW0qSxqozBPA7S"
    "2XxbOiVCsgy4mm2bECd0xM7JUCIsKWU8RBhSi2gDym80wWGVSXUI5pPjTeT58ORL/miqsQQoIzv19OxSh8hZE2czd+48Lbf6"
    "+sC0iXBL8zaTY3NASVpEwvPneZYxYJQn6FMrMBQgQwZaOGQx0+aY8TaWABbK+4lUH9QPtNR5Wg/x4G55N0L9kGQOZ5o4079M"
    "eKkkAnjxhsdSaQ9PrI4AEUsFEmt2ySLZ39iUxwM4VmPYvRmxPJfxaBS4KT7r1Hcbgq6J9PXwevi51+i2zxObnNJ6PT/oPmQO"
    "51EoJTZU6mjy1OMwAbmIlDYENkU7m8KaYEp0AR6KYwn7I4PcDgEIrO5+sZxt3BXG+Khi4ILJM5VPUlCPnh7RXe5ldugXhorH"
    "s2uAg9D9ndHX8T3ntDtlDt3zaWQ7mSYAZ+jCs8w9Qz1RSTnnDh9V6liXxKlcmJH0ArrtV/bNKuC5CYStAeE8Disy2EznkCK2"
    "jYKnUlJjmXitVI1j3E4i3xdLSZLbkOa6fN3Gyxp7kyt4zFQHTZ/xNxHdg5PegV7E2VHgnBrdxFNbDDu6ch4WS1ZJKC4SufrP"
    "TfNsGKJX7cucfgjJvR+jioPHbiNLY3NbuebBgfg9AloyED8UprwW28KAx1j4fd56bJ2nnRdeMMa+o/QhJireGTTRiXtBxUm9"
    "DL8OPnwLd6OzdR88/sF/5/+H/Q4l8sXuYNcHMvqj17wZ9nv9jyOC9msXbNbR8FOTDNu9VnsIf33qNEgD/mzcWuNOtw13Da5F"
    "oznufGqTxsdWp3+Glm6f/hpv38dep9nAE49g8dG43U2881F71GY7D1jBBxa/CYf6xf4X/rA3Sg3qIgl7OrSkr9H6PBo3xu2L"
    "buPAJVpHAYbxLr/BdWHy8FC0azugIYoLEZ44okzyEsiNHYZ7yfdai/7REp6Vzmq+RlJs3nZxe+ImNVr/+jgad/HytDqj/hBu"
    "BvnYGw3azc5Vh2bZJn/h272zCnkjsd1ELq/EDICuC0RRyL8qRI9+2SngYfL9N8m/9wdDSAV9TrP3c0a+4oEsYtTkrC4hR35j"
    "1YiFhFllhdPNaBpFpfaEJvREpSyRNOiSKNglwWOS2A9D8lIZwdyugDriK3/U07QSNSnYk4ntr0JFF/sXjprA3VuzJJswi5TI"
    "JG9pgKvq7oiYGyYOwHul3gSvSHhPpXKHCYCHaDFKj8IVGoH+XVG5RAciOKiVxssBBZJ4WaxOEYvy6sEoH/Bkl7CcIdM6RaC2"
    "jsSKX6ukIx8MvdKHp6hErKPR1FJoCmwqFtS1Y26X9Ncck5Yj8nFCr56RekN7vzBdnFaISW38kBt5HO79rdr82VmnROhuyyTp"
    "5YylhMrBlNDSY70hQV528UR+EyZvbJ4e5Le8FZSR6cOyetYb5SoXQ6b2lsHY40caSJRwaDRatP6RBhk2rOUmmb9fgJmYdSBq"
    "d9luu7Do2+3eHpZ45BeOYQPtoUScP9cg64s5ZDWk8kZvKIf1+x+ZecJSRvDujgQqqfH1Y+pk0sDAxPmEJsZ+etzwJdQ4Adu9"
    "7agDKPLEimLpJKIMaBcgS2kNaZVm/BhzB3N6/l4ksWI+UUMmETWCyUPvinB3qKc8j+tooaje8nLEpo4fS2zFY4hNZgCZAi8r"
    "E5EMNqlFg4TZYqSu/kQskPf+Gzd+h713//jcRCGg8rUCklvA+bvaufuzSAWEHeaUDxaxFFs+mGKVbA6LyetmlTRuZDBdRiFZ"
    "gpbKAdIUutdX36PCArsNuNBEvETkFfHG40I3R4ZU4immfnBv91CKiaaWN00D7xL51wfORMEUgfVq+cyz4lSeGsuP07PeQhLk"
    "PJ5CJJLvjBQ22kRRzGjWGuzmz/PxDXbVIiQtE/yIM8c8ed82jf1kDu+rG32pCqXXZMM5LR0MOLGpoqZZ9j63zmkqGk8ue+tM"
    "2cw1fJ/cGUzfE5k7ofljvNbGbKQq6G/qY+p0IoKIXb2iV/woejETyV6dSBS8jksXiSeS4mm5TTSniU0g+05Bk1Oxkx1He1vM"
    "JRndvIvOpnT+gVZgF92yw67Vc+Vq7fV6dlRnk3xx6k6s0qziWnah7lr1ymxKuw65M/hyPquerGdHXNctlc+5Iqmb5lVDG8rG"
    "vqOTM1ijAH/NB3Y522Abblo/jytgQnHzttHpkqv+sEss0m23Ok2aUCxLI2EhlwXaV1jKADuCC8TLLPFL3IVVKOfzZ83uiP4j"
    "fE++to7qNNSvzJrrwX/YR5O3IUiRNI+RngEM56yS0Vlm9B/DjbDMBUYfL41FtERxM5+eKVRwaDOdnnHRR+2F0rXD255gSjgR"
    "hz2jWGAn1IDOur7OFt7jErsX0zqCLWzE2cwUxFh1GzyAtWe89obWwC0Xf7nYE5aOziiVSBq06QUt21nN11o9td9fDx/4W6Zl"
    "ibud5jJZ0f/OTNKWmDozDqzQRoA671k7DDz1x0srb58tVt52sX3iLZzp2T+QSNJdqGr91MfLvI0zYqhrlmsjIbUEkpACmxI+"
    "2rCFSJurSazjr05R2Mk3hp5wzVQnhePvtJOdXfH1M6zdb9idSnKXWFcL3p1CHI1jOq1hmVch855jvoN7GdbGV2V1Y5FUoEid"
    "F52D7abqSg0cSphtcJjWE6cbWNvOBFsfk7Q58i0jZ7ZoVxevlyzT1DcuxofoIBWvPzjbfA9WLLPNB6ouStZEvZME8+L3SzBP"
    "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"
    "CchW1QAmxf3y99//BywSLa6V4gUA"
)


def _load_backup() -> dict:
    return json.loads(gzip.decompress(base64.b64decode(_BACKUP_B64GZ)).decode())


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Apply the correct Provider Selection Step 7 #172 (5th-choice) "
        "fix to the 6 affected claims on prod; revert the earlier 3rd-choice change. "
        "Verdict stays CLEAN. No LLM, no external files."
    )
    ap.add_argument("--dry-run", action="store_true", help="Preview only (default).")
    ap.add_argument("--apply", action="store_true", help="Commit the changes.")
    opts = ap.parse_args()
    dry = not opts.apply

    for key, val in _PROD_ENV.items():
        os.environ.setdefault(key, val)
    os.environ["NO_LLM"] = "1"
    os.environ["LLM_BACKEND"] = "none"
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    backup = _load_backup()

    import django

    django.setup()

    from django.db import transaction

    from execution_app import trace_builder
    from execution_app.models import ClaimTrace, RuleEvaluation, RuleExecutionRun
    from execution_app.trace_builder import _build_explainability, _iso

    _p("-- Apply Provider Selection Step 7 #172 fix (revert 3rd-choice, apply 5th) --")
    _p(f"  mode        = {'DRY-RUN (no writes)' if dry else 'APPLY (writing)'}")
    _p(f"  PG_HOST     = {os.environ.get('PG_HOST')}")
    _p(f"  PG_DATABASE = {os.environ.get('PG_DATABASE')}")
    _p(
        f"  values      = baked-in blob ({len(_BACKUP_B64GZ)} b64 chars, {len(backup)} runs)"
    )
    _p(f"  claims      = {len(RUNS)} (hardcoded)")

    def _splice_ps(trace_json, ps_entries):
        """Replace the run of Provider Selection entries with the corrected ones,
        preserving every other SOP entry and their relative position."""
        out = []
        inserted = False
        for entry in trace_json:
            if entry.get("sop_name") == PS_TITLE:
                if not inserted:
                    out.extend(ps_entries)
                    inserted = True
                continue  # drop prod's (mistaken) PS entries
            out.append(entry)
        if not inserted:  # no PS entries present (shouldn't happen) -> append
            out.extend(ps_entries)
        return out

    fixed = skipped = failed = 0
    for i, (run_id, claim_id) in enumerate(RUNS.items(), 1):
        b = backup.get(run_id)
        if not b:
            _p(f"[{i}/{len(RUNS)}] {claim_id} run={run_id} SKIP: no baked value")
            skipped += 1
            continue
        run = RuleExecutionRun.objects.filter(id=run_id).first()
        if run is None:
            _p(f"[{i}/{len(RUNS)}] {claim_id} run={run_id} SKIP: run not found on prod")
            skipped += 1
            continue
        if run.claim_id and run.claim_id != claim_id:
            _p(
                f"[{i}/{len(RUNS)}] {claim_id} run={run_id} SKIP: claim mismatch "
                f"(prod={run.claim_id})"
            )
            skipped += 1
            continue

        try:
            if dry:
                e3 = b["evals"].get("step:12:7:3", {})
                e5 = b["evals"].get("step:12:7:5", {})
                _p(
                    f"[{i}/{len(RUNS)}] {claim_id} [WOULD FIX] "
                    f"7:3 -> {e3.get('matched')}/{e3.get('decision_type')} (revert), "
                    f"7:5(#172) -> {e5.get('matched')}/{e5.get('decision_type')} (fix); "
                    f"PS trace entries={len(b['ps_trace_entries'])}"
                )
                fixed += 1
                continue

            with transaction.atomic():
                # 1) evals: revert 7:3, apply 7:5 fix.
                for rk in EVAL_KEYS:
                    vals = b["evals"].get(rk)
                    if not vals:
                        continue
                    ev = RuleEvaluation.objects.filter(run=run, rule_key=rk).first()
                    if ev is None:
                        continue
                    for f in EVAL_FIELDS:
                        if f in vals:
                            setattr(ev, f, vals[f])
                    ev.save(update_fields=[f for f in EVAL_FIELDS if f in vals])

                # 2) trace: splice corrected PS entries, recompute derived fields.
                ct = ClaimTrace.objects.filter(run=run).first()
                if ct and isinstance(ct.trace_json, list):
                    new_trace = _splice_ps(ct.trace_json, b["ps_trace_entries"])
                    final = trace_builder.claim_status(new_trace)
                    if final != "CLEAN":
                        raise RuntimeError(
                            f"refusing: recomputed final_status={final} (expected CLEAN)"
                        )
                    ct.trace_json = new_trace
                    ct.final_status = final
                    ct.explainability_json = _build_explainability(
                        new_trace,
                        str(run.id),
                        run.claim_id,
                        _iso(run.started_at),
                        _iso(run.finished_at),
                        run,
                    )
                    ct.save(
                        update_fields=[
                            "trace_json",
                            "explainability_json",
                            "final_status",
                            "updated_at",
                        ]
                    )
            fixed += 1
            _p(
                f"[{i}/{len(RUNS)}] {claim_id} [FIXED] 7:3 reverted, 7:5(#172) -> Met/"
                f"CONFIRMED; verdict stays CLEAN"
            )
        except Exception as exc:
            failed += 1
            _p(f"[{i}/{len(RUNS)}] {claim_id} run={run_id} FAILED: {exc}")

    _p("------------------------------------------------------------")
    _p(f"Done ({'DRY-RUN' if dry else 'APPLIED'}).")
    _p(f"  claims           = {len(RUNS)}")
    _p(f"  fixed            = {fixed}")
    _p(f"  skipped          = {skipped}")
    _p(f"  failed           = {failed}")
    if dry:
        _p("\nRe-run with --apply to commit.")


if __name__ == "__main__":
    main()
