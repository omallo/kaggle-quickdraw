import numpy as np
import pandas as pd

from utils import read_lines

categories = read_lines("../../quickdraw/categories.txt")
confusion = np.load("./confusion/confusion.npy")
category_confusion_set_mapping = np.load("./out/category_confusion_set_mapping.npy")

for c in range(confusion.shape[0]):
    category_count = confusion[c, :].sum()
    if category_count != 0:
        confusion[c, :] /= category_count

confusion_sets = []
confusion_means = []
for i in range(6):
    confusion_set = read_lines("./out/confusion_set_{}.txt".format(i))
    confusion_sets.append(confusion_set)
    confusion_sum = 0
    for c in confusion_set:
        idx = categories.index(c)
        confusion_sum += confusion[idx, :].sum() - confusion[idx, idx]
    confusion_means.append(confusion_sum / len(confusion_set))
print("means: {}".format(confusion_means))

category_count = {}
for c in categories:
    for cs in confusion_sets:
        if c in cs:
            category_count[c] = category_count.setdefault(c, 0) + 1

for c in categories:
    if category_count[c] > 1:
        print("{}: {}".format(c, category_count[c]))

df = pd.read_csv(
    "./confusion/submission_ensemble_tta.csv",
    index_col="key_id",
    converters={"word": lambda word: [w.replace("_", " ") for w in word.split(" ")]})

cs_match1 = []
cs_match2 = []
cs_match3 = []
cs_matchcs = []
for word in df.word:
    match1 = 0
    match2 = 0
    match3 = 0
    matchcs = 0
    for cs in confusion_sets:  # for cs in [confusion_sets[category_confusion_set_mapping[categories.index(word[0])]]]:
        if word[0] in cs:
            match1 += 1
        if word[0] in cs and word[1] in cs:
            match2 += 1
        if word[0] in cs and word[1] in cs and word[2] in cs:
            match3 += 1
            cs0 = category_confusion_set_mapping[categories.index(word[0])]
            cs1 = category_confusion_set_mapping[categories.index(word[1])]
            cs2 = category_confusion_set_mapping[categories.index(word[2])]
            if cs0 == cs1 and cs1 == cs2:
                matchcs += 1

    cs_match1.append(match1)
    cs_match2.append(match2)
    cs_match3.append(match3)
    cs_matchcs.append(matchcs)

df["cs_match1"] = cs_match1
df["cs_match2"] = cs_match2
df["cs_match3"] = cs_match3
df["cs_matchcs"] = cs_matchcs

df["cs_match_at"] = [3 if m3 > 0 else (2 if m2 > 0 else 1) for m1, m2, m3 in zip(cs_match1, cs_match2, cs_match3)]
