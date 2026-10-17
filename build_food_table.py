"""Usage: python3 build_food_table.py cofid.json food_table.json

Builds food_table.json: per-100 g nutrition from UK CoFID 2021
(McCance & Widdowson) by food code, USDA FDC or product labels where CoFID has no entry,
plus unit weights (g per slice/item/tbsp...) for how the food is actually described.

cofid.json is the CoFID 2021 xlsx (gov.uk, "Composition of foods integrated dataset")
flattened to {food code: {name, kcal, protein_g, fat_g, carbs_g, sugar_g, fiber_g,
sodium_mg}} per 100 g from the "1.3 Proximates" and "1.4 Inorganics" sheets."""
import json, sys
cofid = json.load(open(sys.argv[1]))
K = ("kcal", "protein_g", "carbs_g", "fat_g", "fiber_g", "sugar_g", "sodium_mg")

def C(code):   # CoFID row
    r = cofid[code]; return {k: round(r[k], 1) for k in K}, f"CoFID 2021 {code} ({r['name']})"
def L(kcal, p, c, f, fi=0, su=0, na=0, src=""):   # label / USDA per 100 g
    return dict(zip(K, (kcal, p, c, f, fi, su, na))), src

F = {}
def add(key, per_src, units, aliases=(), default=None):
    per, src = per_src
    F[key] = {"per100": per, "source": src, "units": {"g": 1, **units},
              "default_unit": default or next(iter(units), "g"), "aliases": list(aliases)}

# ── Bread & cereals ──
add("white bread", C("11-1145"), {"slice": 38}, ["toast", "white toast", "sliced bread"])
add("wholemeal bread", C("11-981"), {"slice": 38}, ["brown bread", "brown toast"])
add("sourdough", L(272, 10.8, 51.9, 2.4, 2.2, 4.3, 600, "USDA FDC 172675 (French/Vienna incl. sourdough)"), {"slice": 50}, ["sourdough toast", "sourdough bread"])
add("bread roll", C("11-1006"), {"item": 60}, ["burger bun", "bap", "brown roll", "soft roll"])
add("bagel", C("11-970"), {"item": 85})
add("ciabatta", C("11-969"), {"item": 80, "slice": 30})
add("tortilla wrap", C("11-925"), {"item": 62}, ["wrap", "tortilla"])
add("naan", C("11-973"), {"item": 130})
add("granola", C("11-939"), {"bowl": 70, "serving": 45}, ["crunchy muesli", "crunchy oat cereal"])
add("muesli", C("11-780"), {"bowl": 60, "serving": 45})
add("porridge oats (dry)", C("11-788"), {"bowl": 50, "serving": 40, "cup": 80}, ["oats", "rolled oats", "porridge", "overnight oats"])
add("cornflakes", C("11-742"), {"bowl": 40, "serving": 30})
add("pasta (dry weight)", C("11-716"), {"portion": 100}, ["pasta", "spaghetti", "penne", "linguine", "fusilli"], default="g")
add("pasta (cooked)", C("11-1129"), {"bowl": 250, "portion": 230}, ["cooked pasta", "leftover pasta"])
add("rice (dry weight)", C("11-861"), {"portion": 75}, ["rice"], default="g")
add("rice (cooked)", C("11-862"), {"bowl": 200, "portion": 180}, ["cooked rice", "sticky rice", "boiled rice"])
# ── Dairy & eggs ──
add("whole milk", C("12-596"), {"ml": 1.03, "glass": 258, "pint": 585, "splash": 30}, ["milk", "full fat milk"], default="ml")
add("semi-skimmed milk", C("12-313"), {"ml": 1.03, "glass": 258, "pint": 585, "splash": 30}, ["semi skimmed milk"], default="ml")
add("greek yoghurt", C("12-555"), {"pot": 150, "bowl": 170, "tbsp": 20}, ["greek yogurt", "yoghurt", "yogurt"])
add("cheddar", C("12-346"), {"slice": 20, "handful grated": 30, "tbsp grated": 7}, ["cheddar cheese", "mature cheddar", "cheese", "grated cheese"])
add("mozzarella", C("12-360"), {"ball": 125})
add("halloumi", C("12-496"), {"slice": 25})
add("parmesan", C("12-526"), {"tbsp grated": 5})
add("butter", C("17-685"), {"knob": 10, "tsp": 5, "tbsp": 14, "spread on toast": 7})
add("double cream", C("12-334"), {"tbsp": 15})
add("egg", C("12-940"), {"item": 50}, ["eggs", "boiled egg", "poached egg"])
add("fried egg", C("12-944"), {"item": 55})
add("scrambled eggs", C("12-945"), {"egg's worth": 60}, ["scrambled egg"], default="egg's worth")
add("vanilla ice cream", C("12-508"), {"scoop": 50}, ["ice cream"])
# ── Meat & fish ──
add("back bacon (grilled)", C("19-500"), {"rasher": 25}, ["bacon", "bacon rasher", "rashers"])
add("pork sausage (grilled)", C("19-509"), {"item": 40}, ["sausage", "sausages", "pork sausages"])
add("chicken breast (grilled)", C("18-323"), {"item": 130}, ["chicken breast", "chicken"])
add("chicken thigh (meat)", C("18-319"), {"item": 90}, ["chicken thighs"])
add("beef mince", C("18-037"), {"portion": 125}, ["mince", "ground beef"], default="g")
add("beef burger patty", C("18-042"), {"item": 110}, ["burger", "beef patty"])
add("salmon fillet (baked)", C("16-359"), {"item": 110}, ["salmon", "salmon fillet"])
add("tuna (canned in brine)", C("16-416"), {"can": 102, "half can": 51}, ["tuna"])
add("prawns (cooked)", C("16-389"), {"handful": 60}, ["prawns", "king prawns"])
add("fish fingers (cod, baked)", C("16-405"), {"item": 28}, ["fish fingers"])
# ── Fruit & veg ──
add("banana", C("14-318"), {"item": 120}, ["bananas"])
add("apple", C("14-319"), {"item": 160, "half": 80}, ["apples", "pink lady", "pink lady apple", "apple slices"])
add("orange", C("14-327"), {"item": 130}, ["easy peeler", "satsuma", "clementine"])
add("grapes", C("14-350"), {"handful": 80})
add("strawberries", C("14-324"), {"handful": 80, "punnet": 400})
add("avocado", C("14-386"), {"half": 70, "item": 140})
add("mushrooms (raw weight)", C("13-505"), {"pack": 250, "half pack": 125, "handful": 40}, ["mushrooms"])
add("tomato", C("13-517"), {"item": 85, "cherry tomato": 15}, ["tomatoes"])
add("spinach", C("13-572"), {"bag": 200, "handful": 30})
add("broccoli", C("13-503"), {"portion": 80}, ["tenderstem broccoli"])
add("peas", C("13-536"), {"portion": 80})
add("onion", C("13-499"), {"item": 150})
add("carrot", C("13-496"), {"item": 80})
add("potato (baked)", C("13-491"), {"item": 220}, ["jacket potato", "baked potato"])
add("new potatoes (boiled)", C("13-495"), {"portion": 175}, ["new potatoes", "potatoes"])
add("oven chips", C("13-487"), {"portion": 150}, ["chips", "french fries", "fries"])
add("baked beans", C("13-532"), {"tin": 415, "half tin": 207}, ["beans"])
# ── Condiments & spreads ──
add("mayonnaise", C("17-654"), {"tbsp": 15, "tsp": 5}, ["mayo"])
add("tomato ketchup", C("17-709"), {"tbsp": 15}, ["ketchup"])
add("pesto (green)", C("17-622"), {"tbsp": 15}, ["pesto"])
add("peanut butter", C("14-892"), {"tbsp": 16, "tsp": 6})
add("honey", C("17-050"), {"tsp": 7, "tbsp": 21})
add("nutella", L(539, 6.3, 57.5, 30.9, 0, 56.3, 41, "Label: Ferrero Nutella (Open Food Facts)"), {"tbsp": 15, "tsp": 7}, ["chocolate spread"])
add("olive oil", C("17-038"), {"tbsp": 14, "tsp": 5, "drizzle": 7}, ["oil", "cooking oil", "garlic oil"])
add("houmous", C("13-556"), {"tbsp": 30}, ["hummus"])
add("coleslaw", C("15-635"), {"tbsp": 25, "portion": 50})
# ── Snacks & sports ──
add("flapjack", C("11-814"), {"item": 65}, ["flapjacks"])
add("cereal bar", C("11-819"), {"item": 30}, ["snack bar", "granola bar", "nature valley bar"])
add("protein bar", L(355, 33, 30, 13, 8, 2, 400, "Label (typical Grenade-type 60 g bar; not verified)"), {"item": 60})
add("energy gel", L(145, 0, 37, 0, 0, 1, 20, "Label: SiS GO isotonic 60 ml (not verified)"), {"item": 60})
add("kit kat chunky", L(505, 6.8, 64.8, 25, 0, 52, 40, "Label: Nestlé KitKat Chunky (Open Food Facts)"), {"item": 40}, ["kitkat chunky"])
add("milk chocolate", C("17-648"), {"bar": 45, "square": 6}, ["chocolate"])
add("rich tea biscuit", L(460, 7.2, 71.1, 15.7, 2.5, 20, 280, "Label: McVitie's Rich Tea (Open Food Facts)"), {"item": 8.3}, ["rich tea", "rich tea biscuits"])
add("rice krispies square", L(425, 2.9, 75, 12.1, 1.1, 32.1, 300, "Label: Kellogg's Rice Krispies Squares Marshmallow, 28 g bar (UK product listings)"), {"item": 28}, ["squares", "kelloggs squares", "rice krispies squares", "marshmallow square", "cereal square"])
add("pear", L(57, 0.4, 15.2, 0.1, 3.1, 9.8, 1, "Label/typical: Pear, raw (USDA FDC 169118)"), {"item": 178}, ["pears"])
add("chia seeds", L(486, 16.5, 42.1, 30.7, 34.4, 0, 16, "Label/typical: Chia seeds, dried (USDA FDC 170554)"), {"tbsp": 12, "tsp": 4}, ["chia"], default="tbsp")
add("beetroot", L(43, 1.6, 9.6, 0.2, 2.8, 6.8, 78, "Label/typical: Beetroot, raw (USDA FDC 169145)"), {"item": 50}, ["beets", "beetroots"])
add("blueberries", L(57, 0.7, 14.5, 0.3, 2.4, 10, 1, "Label/typical: Blueberries, raw (USDA FDC 171711)"), {"handful": 50}, ["blueberry"])
add("raspberries", L(52, 1.2, 11.9, 0.7, 6.5, 4.4, 1, "Label/typical: Raspberries, raw (USDA FDC 167755)"), {"handful": 50}, ["raspberry"])
add("blackberries", L(43, 1.4, 9.6, 0.5, 5.3, 4.9, 1, "Label/typical: Blackberries, raw (USDA FDC 173946)"), {"handful": 50}, ["blackberry"])
add("kale", L(49, 4.3, 8.8, 0.9, 3.6, 2.3, 38, "Label/typical: Kale, raw (USDA FDC 168421)"), {"handful": 30}, ["crispy kale (no oil)", "curly kale"])
add("pizza (hawaiian)", L(245, 11.0, 31.0, 8.5, 2.0, 5.0, 540, "Typical ham-and-pineapple pizza, approximate (not a specific brand; whole = ~400 g, a medium takeaway/frozen pizza)"), {"whole": 400, "slice": 50, "half": 200}, ["hawaiian pizza", "hawaiian", "ham and pineapple pizza", "hawaii pizza", "pineapple pizza"], default="whole")
add("ginger nut biscuit", C("11-798"), {"item": 10}, ["ginger snaps", "ginger nuts"])
add("crisps", C("17-671"), {"bag": 25, "sharing bag": 150}, ["walkers crisps", "salt and vinegar crisps"])
add("doughnut (jam)", C("11-849"), {"item": 75}, ["jam doughnut", "donut", "jam donuts"])
add("whey protein", L(384, 69, 18, 3.4, 0, 6, 250, "Label: Bulk Pure Whey (Open Food Facts)"), {"scoop": 30}, ["protein powder", "whey"])
add("yazoo chocolate milk", L(62, 3.2, 8.8, 1.4, 0, 8.6, 50, "Label: Yazoo chocolate (Open Food Facts)"), {"ml": 1.03, "bottle": 412, "small bottle": 206}, ["yazoo", "chocolate milk"], default="ml")
# ── Ready meals / takeaway ──
add("pizza (cheese & tomato)", C("11-936"), {"whole": 380, "slice": 50, "half": 190}, ["pizza", "margherita"], default="whole")

json.dump(F, open(sys.argv[2], "w"), indent=1, ensure_ascii=False)
print(len(F), "foods written")
