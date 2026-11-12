1 | OK | 
2 | OK | 
3 | OK | 
4 | OK | 
5 | OK | 
6 | OK | 
7 | AMBIGUOUS | The `command` format "--slack" cannot be inferred from the question or the one-line description ("slack", "status slack" etc. are equally plausible).
8 | OK | 
9 | AMBIGUOUS | Free-text address: the question writes "Kiyomizu‑cho" with a non-breaking hyphen (U+2011) while gold uses an ASCII hyphen, so a verbatim copy scores 0; the tea-house-to-restaurant mapping is also loose.
10 | OK | 
11 | OK | 
12 | OK | 
13 | OK | Unsure: live_temp is air not water temperature, and a careful person might add weather_warnings or sea forecast day 0; judged OK under rule 1.
14 | OK | 
15 | OK | 
16 | OK | Unsure: height unit is not stated (165 cm vs 1.65 m); passing the numbers as given is the natural reading.
17 | OK | 
18 | OK | 
19 | OK | 
20 | OK | 
21 | OK | Slightly unsure: optional `institution` could be "Stanford" or omitted, but the question gives "Stanford University" verbatim.
22 | OK | 
23 | OK | 
24 | OK | 
25 | OK | 
26 | OK | Slightly unsure: `location` has no description, "Tokyo" vs "Tokyo, Japan" / "Japan" is possible.
27 | WRONG | The question is a how-to about ordering environments in a VR museum tour; navigate_to(city) is only loosely related (rule 3). Unsure: could be read as a disguised "navigate to New York".
28 | OK | 
29 | OK | 
30 | OK | 
31 | OK | 
32 | OK | 
33 | OK | 
34 | OK | 
35 | OK | 
36 | OK | 
37 | OK | 
38 | OK | 
39 | OK | 
40 | OK | 
41 | OK | 
42 | AMBIGUOUS | Coordinates are given and `weather-get_forecast(latitude, longitude)` fits the event location at least as well as the parameterless `get_weather` chosen by gold.
43 | OK | 
44 | OK | 
45 | OK | 
46 | OK | 
47 | AMBIGUOUS | Price "expressed in Bitcoin" may reasonably lead to an extra getTokenPrice(BTC) call for conversion; gold has only USDT.
48 | OK | 
49 | OK | 
50 | OK | 
51 | OK | 
52 | OK | 
53 | OK | 
54 | OK | Air vs water temperature, but it is the only temperature tool (rule 1).
55 | OK | 
56 | AMBIGUOUS | Gold rewrites the code with a newline while the question gives it on one line with "; "; a verbatim copy of the quoted code scores 0.
57 | OK | 
58 | OK | 
59 | OK | 
60 | OK | 
61 | OK | 
62 | OK | 
63 | OK | 
64 | OK | Slightly unsure: get_compound_details is a possible alternative, but "properties" matches solubility/LogP best.
65 | OK | 
66 | OK | 
67 | OK | 
68 | OK | 
69 | OK | 
70 | OK | 
71 | OK | 
72 | OK | 
73 | OK | 
74 | OK | 
75 | OK | 
76 | AMBIGUOUS | Three-step request (discover, add ancestor/2, query); gold has only discover, while another person could also emit exec and query with free-text programs. Arguably WRONG (missing calls) if all steps are expected.
77 | OK | Slightly unsure: full name "Takeshi Nakamura" vs first name only.
78 | OK | 
79 | OK | Unsure: the parameterless weather tool cannot target a route/date/hours, but it is the only tool and the question is about weather (rule 1 over rule 3).
80 | OK | Slightly unsure: "switch location context" maps to navigate_to less directly than a plain navigation request.
81 | OK | 
82 | OK | Unsure: same as 79, parameterless weather tool cannot target Napa Valley or next Saturday.
83 | OK | 
84 | OK | 
85 | OK | 
86 | OK | 
87 | OK | 
88 | OK | 
89 | OK | 
90 | OK | 
91 | OK | 
92 | OK | 
93 | OK | 
94 | OK | 
95 | OK | 
96 | OK | 
97 | OK | 
98 | OK | 
99 | OK | 
100 | OK | 

## Summary

Label counts:

| | OK | WRONG | AMBIGUOUS | Total |
|---|---|---|---|---|
| call (1-85) | 78 | 1 | 6 | 85 |
| refuse (86-100) | 15 | 0 | 0 | 15 |
| all | 93 | 1 | 6 | 100 |

Recurring problem patterns:

- Free-text argument not reproducible by verbatim copy from the question (Unicode hyphen normalised, code reformatted): 9, 56.
- Argument format not inferable from the question or tool description: 7 (clear case); weaker, labelled OK with a note: 16 (unit), 26 (location wording), 77 (full vs first name).
- Parameterless `weather-information-server-get_weather` used for a question that names a specific place/time: 42 (AMBIGUOUS because a coordinate-taking tool is also visible), 79 and 82 (labelled OK under rule 1, but I am unsure; under a strict reading of rule 3 they would be WRONG).
- Multi-step or compound request where the gold covers only part, or where an extra call is reasonable: 76 (only the first of three steps), 47 (BTC-denominated price), 13 (possible extra warnings call, labelled OK).
- Obfuscated/re-skinned question that only loosely matches the tool: 27 (WRONG), 80 (labelled OK, borderline).
- Temperature tool used for "water temperature": 13, 54 (labelled OK under rule 1).
- Refuse items: all 15 are clear; no visible tool is related to the request (86 is the closest: Portugal-only forecast tool vs London).
