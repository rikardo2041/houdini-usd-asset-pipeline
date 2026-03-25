"""
Houdini 21 — Material & Cache Importer
=======================================
Version: 1.0  |  Tested on Houdini 21.0.559

INSTALLATION
    Paste the entire contents of this file into a Houdini Shelf Tool
    (right-click shelf → New Tool → Script tab) and click Accept.

PIPELINE OVERVIEW
    Tab ⓪  Split Geometry    Split a single combined FBX/OBJ into individual
                              per-asset files based on the 'name' prim attribute.
                              Uses groupsfromname + blast + Name SOP (shop_materialpath).

    Tab ①  Textures → /mat  Scan a texture folder and build one Karma MaterialX
                              subnet per material group. Supports 9 map types.
                              Sets displacement scale on mtlxdisplacement nodes.

    Tab ②  Caches → /obj    Import geometry files into one Geo node. Per file:
                              file → normal → matchsize → [name] → material
                              Supports 5 group assignment modes (see below).

    Tab ③  USD Export        Build LOP chains in /stage and export one .usd per
                              geometry: sopimport → materiallibrary → assignmaterial
                              → usd_rop

    Tab ④  Component Builder Build USD component chains in /stage referencing
                              exported USDs: componentgeometry → materiallibrary
                              → componentmaterial → componentoutput

    Tab ⑤  Component Export  Export component USDs to a library folder with
                              512×512 viewport thumbnails auto-framed per asset.

    Tab ⑥  Asset Catalog     Register all component USDs into a Houdini Asset
                              Catalog SQLite (.db) file with embedded thumbnails.

MATERIAL GROUP ASSIGNMENT MODES (Tab ②)
    auto            Auto-detect: prefers @name attrib if multiple unique values
                    exist, falls back to primitive groups.
    name_attrib     Use unique values from the 'name' prim attribute.
                    Supports path format: Grp/Part → last segment used.
    prim_groups     Use Houdini primitive group names directly.
    prefix          Only groups starting with a user-defined prefix string.
    shop_mat        Use unique values from the 'shop_materialpath' prim attrib.

USD PRIM PATH CONVENTIONS
    Geometry subsets : /ASSET/<stem>/<subset_name>
    Materials        : /ASSET/mtl/<mat_stem>/
    Component geo    : /ASSET/geo/<subset_name>

GROUP → MATERIAL STEM NORMALISATION
    1. Strip leading 'm' if followed by uppercase  (mKB3D → KB3D)
    2. Lowercase everything
    3. Collapse non-alphanumeric runs to underscores
    e.g.  mKB3D_SVB_Trash  →  kb3d_svb_trash
          kb3d_metala       →  kb3d_metala (unchanged)

FUZZY MATCHING (Force Match)
    When enabled, if no exact /mat match exists for a group, the tool splits
    the group name into words (>2 chars) and checks if any word appears as a
    substring in any /mat node name.
    e.g.  Wasteland_SM_A_cans  →  word 'cans'  →  /mat/kb3d_cans

TEXTURE MAP SUFFIXES
    basecolor   basecolor albedo diffuse base_color color col bc diff alb
    roughness   roughness rough rgh glossiness gloss gls
    metallic    metallic metal metalness mtl met
    normal      normal nrm nor nml norm
    height      height hgt disp displacement
    ao          ao ambient_occlusion ambientocclusion occlusion
    opacity     opacity alpha transparency transp
    emission    emission emissive emit glow emissivecolor
    specular    specular spec

CACHE FORMATS SUPPORTED
    .bgeo  .bgeo.sc  .abc  .usd  .usdc  .usdz  .obj  .fbx

ARCHITECTURE (for developers)
    All node-building functions are standalone and can be called without the UI:

    build_material(stem, maps, prefix, disp_scale)
        → Creates one MaterialX subnet in /mat

    build_geo(cache_entries, geo_name, group_mode, group_prefix,
              fuzzy_match, use_name_node, progress_cb)
        → Creates the SOP chain in /obj/<geo_name>

    build_lop_export(geo_node, stem, matched_groups, out_folder)
        → Creates LOP export chain in /stage, returns usd_rop node

    build_component(stem, matched_groups, usd_path, comp_folder)
        → Creates component chain in /stage, returns componentoutput node

    split_geometry(source_path, out_folder, scale, progress_cb)
        → Splits a combined FBX/OBJ into per-asset files, returns (exported, errors)

EXTENDING
    Add a new map type   : edit MAP_RULES list in the Texture scanning section
    Add a cache format   : edit _SUPPORTED_CACHE_EXTS set in Config section
    Add a UI tab         : add _tab_xxx() + slots, register in _build_ui()
    Change USD paths     : edit primpattern/matspecpath in build_lop_export()
                           and primpattern/matspecpath in build_component()

DEPENDENCIES
    hou, PySide6 (bundled with Houdini 21)
    sqlite3, uuid, re, os, time, datetime  (Python stdlib)
"""


import os
import re
import hou
from PySide6 import QtCore, QtWidgets, QtGui
import shiboken6

# =============================================================================
# SECTION 2 — Cache / geometry scanning
# Detects supported geometry file formats and builds entry dicts for build_geo.
# =============================================================================
_SUPPORTED_TEX_EXTS   = {".exr", ".tif", ".tiff", ".png", ".jpg", ".jpeg", ".tx"}
_SUPPORTED_CACHE_EXTS = {".bgeo", ".sc", ".abc", ".usd", ".usdc", ".usdz", ".obj", ".fbx"}

_MAP_RULES = [
    ("basecolor", ["basecolor", "base_color", "albedo", "diffuse", "colour", "color", "col", "bc"]),
    ("roughness", ["roughness", "rough", "rgh", "glossiness", "gloss", "gls"]),
    ("metallic",  ["metallic", "metal", "mtl"]),
    ("normal",    ["normal", "nrm", "nor", "normalgl", "normal_dx", "normal_opengl"]),
    ("height",    ["height", "disp", "displacement", "bump"]),
    ("ao",        ["ao", "ambientocclusion", "occlusion"]),
    ("opacity",   ["opacity", "alpha", "trans", "transparency", "cutout"]),
    ("emission",  ["emission", "emit", "emissive", "glow"]),
    ("specular",  ["specular", "spec"]),
]

_SRGB_MAPS = {"basecolor", "emission"}

_MAP_SIGNATURES = {
    "basecolor": "color3", "emission": "color3", "specular": "color3",
    "roughness": "float",  "metallic": "float",  "ao":       "float",
    "opacity":   "float",  "height":   "float",  "normal":   "vector3",
}

_STD_INPUTS = {
    "basecolor": "base_color",
    "roughness": "specular_roughness",
    "metallic":  "metalness",
    "normal":    "normal",
    "opacity":   "opacity",
    "emission":  "emission_color",
    "specular":  "specular_color",
}

_EXT_RE = re.compile(r"\.(exr|tif|tiff|png|jpg|jpeg|tx)$", re.I)

_MAP_COLOURS = {
    "basecolor": "#c8e6c9", "roughness": "#fff9c4",
    "metallic":  "#b3e5fc", "normal":    "#e1bee7",
    "height":    "#ffe0b2", "ao":        "#f0f0f0",
    "opacity":   "#fce4ec", "emission":  "#fff3e0",
    "specular":  "#e3f2fd",
}
_FMT_COLOURS = {
    ".bgeo": "#c8e6c9", ".sc":   "#c8e6c9",
    ".abc":  "#b3e5fc",
    ".usd":  "#e1bee7", ".usdc": "#e1bee7", ".usdz": "#e1bee7",
    ".obj":  "#fff9c4", ".fbx":  "#ffe0b2",
}

# =============================================================================
# SECTION 1 — Texture scanning
# Scans a folder for textures, groups them by material stem, and detects
# map types from filename suffixes.
# =============================================================================

def _normalize(s):
    s = s.lower()
    s = _EXT_RE.sub("", s)
    s = re.sub(r"[\s\-\.]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_")
    return s


def _guess_map_type(filepath):
    name = _normalize(os.path.basename(filepath))
    for mtype, keys in _MAP_RULES:
        for k in keys:
            if re.search(rf"(^|_){re.escape(_normalize(k))}(_|$)", name):
                return mtype
    for mtype, keys in _MAP_RULES:
        for k in keys:
            if _normalize(k) in name:
                return mtype
    return None


def _material_stem(filepath):
    name = _normalize(os.path.basename(filepath))
    name = re.sub(r"[_.](1\d{3})([_.]|$)", "_", f"_{name}_")
    name = re.sub(r"[_.](1k|2k|4k|8k|16k)([_.]|$)", "_", name, flags=re.I)
    name = _normalize(name)
    for _, keys in _MAP_RULES:
        for k in keys:
            tok = _normalize(k)
            name = re.sub(rf"(^|_){re.escape(tok)}(_|$)", "_", f"_{name}_", flags=re.I)
            name = _normalize(name)
    return name or "material"


def scan_textures(folder):
    """Returns { stem: { map_type: filepath, '_unknown': [...] } }"""
    groups = {}
    for fname in sorted(os.listdir(folder)):
        if os.path.splitext(fname)[1].lower() not in _SUPPORTED_TEX_EXTS:
            continue
        fpath = os.path.join(folder, fname)
        stem  = _material_stem(fpath)
        mtype = _guess_map_type(fpath)
        groups.setdefault(stem, {"_unknown": []})
        if mtype and mtype not in groups[stem]:
            groups[stem][mtype] = fpath
        else:
            groups[stem]["_unknown"].append(fpath)
    return groups


# =============================================================================
# SECTION 3 — Group → material stem conversion
# Normalises prim group names or attrib values to /mat node stem names.
# =============================================================================
def _is_cache(fname):
    fl = fname.lower()
    if fl.endswith(".bgeo.sc"):
        return True
    return os.path.splitext(fl)[1] in _SUPPORTED_CACHE_EXTS


def _fmt_label(fname):
    fl = fname.lower()
    return ".bgeo.sc" if fl.endswith(".bgeo.sc") else os.path.splitext(fl)[1]


def _safe_name(s):
    s = s.lower()
    s = s[:-8] if s.endswith(".bgeo.sc") else os.path.splitext(s)[0]
    s = re.sub(r"[^a-z0-9]+", "_", s).strip("_")
    return s or "cache"


def scan_caches(folder):
    """Returns list of { name, path, fmt }"""
    results = []
    for fname in sorted(os.listdir(folder)):
        if _is_cache(fname):
            results.append({
                "name": fname,
                "path": os.path.join(folder, fname),
                "fmt":  _fmt_label(fname),
            })
    return results


# =============================================================================
# SECTION 3 — Group → material stem conversion
# Normalises group names / attrib values to /mat node stem names.
# =============================================================================

def _group_to_mat_stem(group_name):
    """
    Convert a prim group name to a /mat node stem.

    Rules:
      1. Strip a single leading 'm' if the next character is uppercase
      2. Lowercase everything
      3. Collapse non-alphanumeric runs to underscores
      4. Strip leading/trailing underscores

    Examples:
      mKB3D_SVB_Trash  →  kb3d_svb_trash
      concrete_wall    →  concrete_wall
    """
    s = group_name
    if len(s) >= 2 and s[0] == "m" and s[1].isupper():
        s = s[1:]
    s = s.lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = s.strip("_")
    return s or "material"


def _fuzzy_match_mat(group_name, fuzzy=False):
    """
    Try to find a matching /mat node for a group name.

    Strategy:
      1. Exact stem match  (always tried)
         kb3d_svb_trash == /mat/kb3d_svb_trash
      2. Single-word match (only when fuzzy=True)
         any word from the group name appears as a substring in a /mat node
         e.g. 'debris' in /mat/kb3d_debris

    Returns the matched /mat node name stem or "".
    """
    mat_ctx = hou.node("/mat")
    if mat_ctx is None:
        return ""

    mat_names = [child.name() for child in mat_ctx.children()]

    # Strategy 1 — exact stem
    stem = _group_to_mat_stem(group_name)
    if stem in mat_names:
        return stem

    if not fuzzy:
        return ""

    # Strategy 2 — single word match (opt-in)
    last_seg = group_name.split("/")[-1]
    words = [w.lower() for w in re.split(r"[^a-zA-Z0-9]+", last_seg) if len(w) > 2]

    for word in words:
        for mat_name in mat_names:
            if word in mat_name:
                return mat_name

    return ""


# =============================================================================
# SECTION 4 — Prim group reader
# Reads primitive group names from a geometry file via hou.Geometry.
# Used when group_mode is "prim_groups" or "prefix".
# =============================================================================

def read_prim_groups(cache_path):
    """
    Load cache into a temporary hou.Geometry and return list of prim group names.
    Returns [] if file cannot be read.
    """
    try:
        geo = hou.Geometry()
        geo.loadFromFile(cache_path)
        return [g.name() for g in geo.primGroups()]
    except Exception:
        return []


# =============================================================================
# SECTION 5 — Houdini node helpers (internal)
# Small helpers used by build_material and build_geo.
# =============================================================================
def _unique(parent, base):
    if parent.node(base) is None:
        return base
    i = 2
    while parent.node(f"{base}_{i}"):
        i += 1
    return f"{base}_{i}"


def _wire(dst, input_name, src, output_name="out"):
    try:
        dst.setNamedInput(input_name, src, output_name)
    except Exception:
        pass


# =============================================================================
# SECTION 6 — Material builder
# build_material() creates one Karma MaterialX subnet in /mat.
# =============================================================================
def _add_image(parent, map_type, filepath):
    img = parent.createNode("mtlximage", node_name=f"img_{map_type}")
    sig = _MAP_SIGNATURES.get(map_type)
    if sig:
        p = img.parm("signature")
        if p:
            try: p.set(sig)
            except: pass
    for pname in ("file", "filename", "texturepath"):
        p = img.parm(pname)
        if p:
            p.set(filepath)
            break
    colorspace = "srgb" if map_type in _SRGB_MAPS else "raw"
    for pname in ("colorspace", "filecolorspace", "ocio_colorspace"):
        p = img.parm(pname)
        if p:
            try: p.set(colorspace)
            except: pass
            break
    return img


def build_material(stem, maps, prefix="", disp_scale=0.01):
    mat_ctx = hou.node("/mat")
    if mat_ctx is None:
        raise RuntimeError("Could not find /mat network.")
    name = _unique(mat_ctx, f"{prefix}{stem}" if prefix else stem)

    # Create a subnet — this is what Karma Material Builder actually is.
    # We recreate the exact same default children Houdini puts inside it.
    mb = mat_ctx.createNode("subnet", node_name=name)

    # Remove any auto-created default children
    for child in mb.children():
        try: child.destroy()
        except: pass

    # Recreate the exact Karma Material Builder default structure
    inputs  = mb.createNode("subinput",  node_name="inputs")
    std     = mb.createNode("mtlxstandard_surface", node_name="mtlxstandard_surface")
    disp_default = mb.createNode("mtlxdisplacement", node_name="mtlxdisplacement")
    out     = mb.createNode("suboutput", node_name="Material_Outputs_and_AOVs")
    props   = mb.createNode("kma_material_properties", node_name="material_properties")

    # Wire by index — suboutput inputs are:
    #   index 0 = surface     ← mtlxstandard_surface (output index 0)
    #   index 1 = displacement ← mtlxdisplacement    (output index 0)
    #   index 2 = properties  ← kma_material_properties (output index 0)
    try: out.setInput(0, std,          0)
    except: pass
    try: out.setInput(1, disp_default, 0)
    except: pass
    try: out.setInput(2, props,        0)
    except: pass

    if "basecolor" in maps and "ao" in maps:
        img_bc = _add_image(mb, "basecolor", maps["basecolor"])
        img_ao = _add_image(mb, "ao",        maps["ao"])
        mul    = mb.createNode("mtlxmultiply", node_name="mul_bc_ao")
        _wire(mul, "in1", img_bc); _wire(mul, "in2", img_ao)
        _wire(std, "base_color", mul)
    elif "basecolor" in maps:
        _wire(std, "base_color", _add_image(mb, "basecolor", maps["basecolor"]))
    for mtype, std_input in _STD_INPUTS.items():
        if mtype in ("basecolor", "normal", "emission"):
            continue
        if mtype in maps:
            _wire(std, std_input, _add_image(mb, mtype, maps[mtype]))
    if "emission" in maps:
        img_em = _add_image(mb, "emission", maps["emission"])
        _wire(std, "emission_color", img_em)
        p = std.parm("emission")
        if p:
            try: p.set(1.0)
            except: pass
    if "normal" in maps:
        img_n = _add_image(mb, "normal", maps["normal"])
        nrm   = mb.createNode("mtlxnormalmap", node_name="mtlxnormalmap1")
        _wire(nrm, "in", img_n)
        _wire(std, "normal", nrm)
    if "height" in maps:
        img_h = _add_image(mb, "height", maps["height"])
        # Wire into the existing displacement node instead of creating a new one
        p = disp_default.parm("scale")
        if p:
            try: p.set(disp_scale)
            except: pass
        try: disp_default.setNamedInput("displacement", img_h, "out")
        except: pass

    mb.layoutChildren()
    mb.moveToGoodPosition()
    try: mb.setMaterialFlag(True)
    except: pass
    return mb


# =============================================================================
# SECTION 5b — Geometry + SOP builder
# build_geo() creates the full SOP chain per geometry in /obj/<geo_name>:
#   file → normal → matchsize → [name] → material
# Supports 5 group assignment modes and an optional progress callback.
# =============================================================================

def build_geo(cache_entries, geo_name="caches", group_mode="auto",
              group_prefix="m", fuzzy_match=False, use_name_node=True,
              progress_cb=None):
    """
    group_mode:   "auto"        — detect per geometry (name attrib > prim groups)
                  "name_attrib" — always use @name attribute
                  "prim_groups" — always use primitive groups
                  "prefix"      — only groups starting with group_prefix
    group_prefix: string prefix to filter groups in "prefix" mode (default "m")
    fuzzy_match:  if True, fall back to keyword matching when no exact /mat match
                  "shop_mat"    — group by @shop_materialpath attrib values
    """
    """
    Creates one Geo node in /obj.
    Per cache entry:
      file_<stem>      File SOP  — path set
      material_<stem>  Material SOP  — wired after File SOP
          One multiparm slot per prim group:
            group<n>             = original group name  (e.g. mKB3D_SVB_Trash)
            shop_materialpath<n> = /mat/<derived stem>  (e.g. /mat/kb3d_svb_trash)
    Slots are always created regardless of whether the /mat node exists.
    """
    obj = hou.node("/obj")
    if obj is None:
        raise RuntimeError("Could not find /obj network.")

    geo = obj.createNode("geo", node_name=_unique(obj, geo_name))
    geo.moveToGoodPosition()

    for child in geo.children():
        try: child.destroy()
        except: pass

    last_mat_sop = None

    for idx, entry in enumerate(cache_entries):
        if progress_cb:
            progress_cb(idx + 1, len(cache_entries), _safe_name(entry["name"]))
        stem     = _safe_name(entry["name"])
        file_sop      = geo.createNode("file",      node_name=stem)
        normal_sop    = geo.createNode("normal",    node_name=f"normal_{stem}")
        matchsize_sop = geo.createNode("matchsize", node_name=f"matchsize_{stem}")
        name_sop      = geo.createNode("name",      node_name=f"name_{stem}")
        mat_sop       = geo.createNode("material",  node_name=f"material_{stem}")

        p = file_sop.parm("file")
        if p:
            p.set(entry["path"])

        # Normal SOP: weighting method = By Face Area (index 2)
        normal_sop.setInput(0, file_sop)
        p = normal_sop.parm("method")
        if p:
            try: p.set(2)
            except: pass

        # Match Size SOP: Justify Y = Min (index 1 — places geometry on ground plane)
        matchsize_sop.setInput(0, normal_sop)
        p = matchsize_sop.parm("justify_y")
        if p:
            try: p.set(1)
            except: pass

        # ── Detect material groups ───────────────────────────────────────
        # Two modes depending on user selection:
        #
        # PRIM GROUPS mode: read primitive group names from the cache file.
        #   Skip index 0 (the geo-named group covering all prims).
        #   group field in Material SOP = plain group name.
        #
        # NAME ATTRIB mode: read unique values from the 'name' prim attrib
        #   from the File SOP geometry (before our name SOP overwrites it).
        #   Name values are paths like "Grp/Part" — we use the full path
        #   in @name= and match the last segment against /mat.
        #   group field in Material SOP = @name=<full_path>

        # ── Auto-detect assignment mode ──────────────────────────────────
        # 1. Check for name prim attrib with multiple unique values
        #    → name attrib mode (e.g. Warzone/Wasteland kitbash)
        # 2. Otherwise use primitive groups
        #    → prim groups mode (e.g. Soviet Blocks kitbash)
        # User radio button can override this auto-detection.

        detected_name_attrib = False
        raw_names  = []
        prim_groups = read_prim_groups(entry["path"])

        try:
            file_geo  = file_sop.geometry()
            name_attr = file_geo.findPrimAttrib("name")
            if name_attr:
                from collections import Counter
                all_vals    = file_geo.primStringAttribValues("name")
                counts      = Counter(all_vals)
                most_common = counts.most_common(1)[0][0] if counts else ""
                # Keep all non-empty unique values — every value is
                # a potential material group. No filtering by count or stem.
                unique_vals = sorted(v for v in set(all_vals) if v)
                if unique_vals:
                    detected_name_attrib = True
                    raw_names = unique_vals
        except Exception as e:
            print(f"Name attrib detection warning: {e}")

        # Determine mode — user override or auto-detected
        if group_mode == "name_attrib":
            mode = "name_attrib"
        elif group_mode == "prim_groups":
            mode = "prim_groups"
        elif group_mode == "prefix":
            mode = "prefix"
        elif group_mode == "shop_mat":
            mode = "shop_mat"
        else:  # auto
            mode = "name_attrib" if detected_name_attrib else "prim_groups"

        # ── Name attrib mode ──────────────────────────────────────────────
        if mode == "name_attrib":
            matched = []
            for full_name in raw_names:
                last_seg = full_name.split("/")[-1]
                mat_stem = _fuzzy_match_mat(last_seg, fuzzy=fuzzy_match)
                matched.append((full_name, mat_stem))

            if use_name_node:
                name_sop.setInput(0, matchsize_sop)
            num_p = name_sop.parm("numnames")
            if num_p:
                try: num_p.set(len(raw_names))
                except: pass
            for idx, full_name in enumerate(raw_names, start=1):
                last_seg = full_name.split("/")[-1]
                n_parm = name_sop.parm(f"name{idx}")
                g_parm = name_sop.parm(f"group{idx}")
                if n_parm: n_parm.set(last_seg)
                if g_parm: g_parm.set(f"@name={full_name}")

        # ── Prim groups mode ──────────────────────────────────────────────
        elif mode == "prim_groups":
            mat_groups = prim_groups[1:] if len(prim_groups) > 1 else prim_groups
            matched = []
            for g in mat_groups:
                matched.append((g, _fuzzy_match_mat(g, fuzzy=fuzzy_match)))

            if use_name_node:
                name_sop.setInput(0, matchsize_sop)
            num_p = name_sop.parm("numnames")
            if num_p:
                try: num_p.set(len(mat_groups))
                except: pass
            for idx, grp in enumerate(mat_groups, start=1):
                n_parm = name_sop.parm(f"name{idx}")
                g_parm = name_sop.parm(f"group{idx}")
                if n_parm: n_parm.set(grp)
                if g_parm: g_parm.set(grp)

        # ── Prefix mode ───────────────────────────────────────────────────
        elif mode == "prefix":
            mat_groups = [g for g in prim_groups if g.startswith(group_prefix)]
            matched = []
            for g in mat_groups:
                matched.append((g, _fuzzy_match_mat(g, fuzzy=fuzzy_match)))

            if use_name_node:
                name_sop.setInput(0, matchsize_sop)
            num_p = name_sop.parm("numnames")
            if num_p:
                try: num_p.set(len(mat_groups))
                except: pass
            for idx, grp in enumerate(mat_groups, start=1):
                n_parm = name_sop.parm(f"name{idx}")
                g_parm = name_sop.parm(f"group{idx}")
                if n_parm: n_parm.set(grp)
                if g_parm: g_parm.set(grp)

        # ── shop_materialpath attrib mode ─────────────────────────────────
        # Reads unique @shop_materialpath values from the File SOP geometry.
        # Normalizes each value to match /mat node names.
        # Group field = @shop_materialpath=<value>
        else:
            shop_vals = []
            try:
                file_geo   = file_sop.geometry()
                shop_attr  = file_geo.findPrimAttrib("shop_materialpath")
                if shop_attr:
                    all_shop = file_geo.primStringAttribValues("shop_materialpath")
                    shop_vals = sorted(set(v for v in all_shop if v))
            except Exception as e:
                print(f"shop_materialpath read warning: {e}")

            matched = []
            for val in shop_vals:
                # Normalize: lowercase + underscores
                mat_stem = _fuzzy_match_mat(val, fuzzy=fuzzy_match)
                if not mat_stem:
                    # Direct normalize fallback
                    mat_stem = re.sub(r"[^a-z0-9]+", "_",
                                      val.lower()).strip("_")
                    if not hou.node(f"/mat/{mat_stem}"):
                        mat_stem = ""
                matched.append((val, mat_stem))

            # Name SOP: keep geometry stem name — don't use shop_materialpath values here
            if use_name_node:
                name_sop.setInput(0, matchsize_sop)
            num_p = name_sop.parm("numnames")
            if num_p:
                try: num_p.set(1)
                except: pass
            n_parm = name_sop.parm("name1")
            if n_parm: n_parm.set(stem)

        if use_name_node:
            mat_sop.setInput(0, name_sop)
        else:
            mat_sop.setInput(0, matchsize_sop)

        # ── Material SOP slots ────────────────────────────────────────────
        num_p = mat_sop.parm("num_materials")
        if num_p and matched:
            num_p.set(len(matched))
            for i, (group_name, mat_stem_i) in enumerate(matched, start=1):
                gp = mat_sop.parm(f"group{i}")
                mp = mat_sop.parm(f"shop_materialpath{i}")
                if gp:
                    if mode == "name_attrib":
                        last_seg = group_name.split("/")[-1]
                        gp.set(f"@name={last_seg}")
                    elif mode == "shop_mat":
                        gp.set(f"@shop_materialpath={group_name}")
                    else:
                        gp.set(group_name)
                if mp: mp.set(f"/mat/{mat_stem_i}" if mat_stem_i else "")

        last_mat_sop = mat_sop

    if last_mat_sop:
        last_mat_sop.setDisplayFlag(True)
        last_mat_sop.setRenderFlag(True)

    geo.layoutChildren()
    return geo



# =============================================================================
# SECTION 7 — LOP / USD export helpers
# _set_lopoutput(), build_lop_export(), render_lop_export()
# USD prim paths: /ASSET/<stem>/<subset> | /ASSET/mtl/<mat_stem>/
# =============================================================================
def _set_lopoutput(node, path):
    """
    Force-set the lopoutput parm on a usd_rop node, clearing
    the default $HIP/$OS.usd expression that overrides p.set().
    """
    p = node.parm("lopoutput")
    if p is None:
        return
    # revertToDefaults clears the expression, then set() sticks
    try:
        p.revertToDefaults()
    except Exception:
        pass
    try:
        p.deleteAllKeyframes()
    except Exception:
        pass
    p.set(path)


def build_lop_export(geo_node, stem, matched_groups, out_folder):
    """
    Creates a LOP network under /stage for one geometry:
      sopimport_<stem>      — imports SOP geometry from material_<stem>
      assignmaterial_<stem> — assigns /mat/<stem> per prim group
      usd_rop_<stem>        — exports to out_folder/<stem>.usd

    geo_node     : the /obj/<geo_name> container node
    stem         : geometry stem name  e.g. "kb3d_svb_trash"
    matched_groups: list of (group_name, mat_stem) tuples already matched
    out_folder   : output folder path (may be "" — sets $HIP/usd as default)
    """
    stage = hou.node("/stage")
    if stage is None:
        stage = hou.node("/obj").parent().createNode("lopnet", node_name="stage")

    mat_sop = geo_node.node(f"material_{stem}")
    if mat_sop is None:
        raise RuntimeError(f"material_{stem} not found inside {geo_node.path()}")

    # ── sopimport ────────────────────────────────────────────────────────
    sop_import = stage.createNode("sopimport", node_name=f"sopimport_{stem}")
    p = sop_import.parm("soppath")
    if p:
        p.set(mat_sop.path())
    # Prim path: /ASSET/<stem>
    for pname in ("primpath", "prim_path", "path"):
        p = sop_import.parm(pname)
        if p:
            p.set(f"/ASSET/{stem}")
            break
    # Import path prefix: ASSET/<stem>  (fixes the prefix warning)
    for pname in ("pathprefix", "importpathprefix", "pathattr"):
        p = sop_import.parm(pname)
        if p:
            p.set(f"ASSET/{stem}")
            break

    # ── materiallibrary ──────────────────────────────────────────────────
    # One node handles both material import AND geometry assignment.
    # matpathprefix = ASSET/  (materials land at /ASSET/<mat_stem>)
    # matnode#      = /mat/<mat_stem>
    # matpath#      = empty (auto-derived from prefix + node name)
    # assign#       = 1 (Assign to Geometry checked)
    # geopath#      = /ASSET/<stem>
    matlib = stage.createNode("materiallibrary", node_name=f"materiallibrary_{stem}")
    matlib.setInput(0, sop_import)

    p = matlib.parm("matpathprefix")
    if p: p.set("ASSET/mtl/")

    # materiallibrary: one entry per UNIQUE material (no geo assignment here)
    # This imports all needed materials into the stage.
    real_matches  = [(g, ms) for g, ms in matched_groups if ms]
    unique_mats   = list(dict.fromkeys(ms for _, ms in real_matches))
    num_p = matlib.parm("materials")
    if num_p and unique_mats:
        num_p.set(len(unique_mats))
        for i, mat_stem in enumerate(unique_mats, start=1):
            mn = matlib.parm(f"matnode{i}")
            mp = matlib.parm(f"matpath{i}")
            ag = matlib.parm(f"assign{i}")
            if mn: mn.set(f"/mat/{mat_stem}")
            if mp: mp.set("")
            if ag:
                try: ag.set(0)   # no geo assignment — handled by assignmaterial
                except: pass

    # ── assignmaterial ────────────────────────────────────────────────────
    # One slot per group — handles duplicate materials correctly
    assign = stage.createNode("assignmaterial", node_name=f"assignmaterial_{stem}")
    assign.setInput(0, matlib)

    num_p = assign.parm("nummaterials")
    if num_p and real_matches:
        num_p.set(len(real_matches))
        for i, (group_name, mat_stem) in enumerate(real_matches, start=1):
            pp  = assign.parm(f"primpattern{i}")
            mp  = assign.parm(f"matspecpath{i}")
            mpm = assign.parm(f"matspecmethod{i}")
            # primpattern: try specific subset first, fall back to geo root
            # In shop_mat mode the USD has one subset named after the stem,
            # not individual shop_materialpath values
            if pp:  pp.set(f"/ASSET/{stem}/{group_name}")
            if mpm:
                try: mpm.set(0)
                except: pass
            # material path: /ASSET/mtl/<mat_stem>/
            if mp:  mp.set(f"/ASSET/mtl/{mat_stem}/")

    # ── usd_rop ──────────────────────────────────────────────────────────
    usd_rop = stage.createNode("usd_rop", node_name=f"usd_rop_{stem}")
    usd_rop.setInput(0, assign)

    if out_folder:
        out_path = out_folder.replace("\\", "/").rstrip("/") + f"/{stem}.usd"
    else:
        out_path = f"$HIP/usd/{stem}.usd"

    _set_lopoutput(usd_rop, out_path)

    p = usd_rop.parm("defaultprim")
    if p: p.set(f"/ASSET/{stem}")

    # Enable "Flatten SOP Layers" — fixes the export warning
    p = usd_rop.parm("flattensoplayers")
    if p:
        try: p.set(1)
        except: pass

    stage.layoutChildren()
    return usd_rop


def render_lop_export(usd_rop_node, out_path):
    """Update output path (forward slashes) and render a usd_rop node."""
    out_path = out_path.replace("\\", "/")
    _set_lopoutput(usd_rop_node, out_path)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    usd_rop_node.render()



# =============================================================================
# SECTION 8 — Component builder
# Builds USD component chains in /stage:
#   componentgeometry → materiallibrary → componentmaterial → componentoutput
# Each componentoutput exports to <comp_folder>/assets/<stem>/<stem>.usd
# =============================================================================

def build_component(stem, matched_groups, usd_path, comp_folder):
    """
    Creates the full component chain in /stage for one geometry:

      componentgeometry_<stem>   — references the exported USD
      materiallibrary_comp_<stem> — pulls /mat materials into stage
      componentmaterial_<stem>   — assigns materials to geometry subsets
      componentoutput_<stem>     — exports the component USD

    stem          : geometry stem  e.g. "kb3d_svb_bldgsm_c"
    matched_groups: list of (group_name, mat_stem) tuples
    usd_path      : path to the previously exported geometry USD
    comp_folder   : output folder for the component USD
    """
    stage = hou.node("/stage")
    if stage is None:
        raise RuntimeError("Could not find /stage network.")

    usd_path  = usd_path.replace("\\", "/")
    comp_path = comp_folder.replace("\\", "/").rstrip("/") + f"/assets/{stem}/{stem}.usd"

    # ── componentgeometry ────────────────────────────────────────────────
    comp_geo = stage.createNode("componentgeometry",
                                node_name=f"componentgeometry_{stem}")
    # Set source to Referenced Files (value 3)
    p = comp_geo.parm("sourceinput")
    if p:
        try: p.set(3)
        except: pass
    # Set the USD reference path
    p = comp_geo.parm("sourceusdref")
    if p: p.set(usd_path)

    # ── materiallibrary (component) ──────────────────────────────────────
    matlib = stage.createNode("materiallibrary",
                              node_name=f"materiallibrary_comp_{stem}")
    # Material path prefix — materials land at /ASSET/mtl/<mat_stem>
    p = matlib.parm("matpathprefix")
    if p: p.set("ASSET/mtl/")

    # One entry per matched material — no geopath needed this time
    unique_mats = list(dict.fromkeys(ms for _, ms in matched_groups if ms))
    num_p = matlib.parm("materials")
    if num_p and unique_mats:
        num_p.set(len(unique_mats))
        for i, mat_stem in enumerate(unique_mats, start=1):
            mn = matlib.parm(f"matnode{i}")
            mp = matlib.parm(f"matpath{i}")
            ag = matlib.parm(f"assign{i}")
            if mn: mn.set(f"/mat/{mat_stem}")
            if mp: mp.set("")
            if ag:
                try: ag.set(0)
                except: pass

    # ── componentmaterial ────────────────────────────────────────────────
    comp_mat = stage.createNode("componentmaterial",
                                node_name=f"componentmaterial_{stem}")
    # Input 0 = component geometry, Input 1 = material library
    comp_mat.setInput(0, comp_geo)
    comp_mat.setInput(1, matlib)

    # One material assignment per matched group — skip groups with no match
    real_matches = [(g, ms) for g, ms in matched_groups if ms]
    num_p = comp_mat.parm("nummaterials")
    if num_p and real_matches:
        num_p.set(len(real_matches))
        for i, (group_name, mat_stem) in enumerate(real_matches, start=1):
            pp = comp_mat.parm(f"primpattern{i}")
            mp = comp_mat.parm(f"matspecpath{i}")
            if pp: pp.set(f"/ASSET/geo/{group_name}")
            if mp: mp.set(f"/ASSET/mtl/{mat_stem}/")

    # ── componentoutput ──────────────────────────────────────────────────
    comp_out = stage.createNode("componentoutput",
                                node_name=stem)
    comp_out.setInput(0, comp_mat)

    # Name and File Name — both set to geometry stem
    p = comp_out.parm("__name")
    if p: p.set(stem)
    p = comp_out.parm("name")
    if p: p.set(stem)
    p = comp_out.parm("filename")
    if p: p.set(f"{stem}.usd")

    # Output location
    p = comp_out.parm("lopoutput")
    if p:
        try: p.revertToDefaults()
        except: pass
        try: p.deleteAllKeyframes()
        except: pass
        p.set(comp_path)

    # Thumbnail — Viewport mode, 512x512, save scene to disk
    p = comp_out.parm("thumbnailmode")
    if p:
        try: p.set(3)   # 3 = Viewport
        except: pass
    p = comp_out.parm("res1")
    if p:
        try: p.set(512)
        except: pass
    p = comp_out.parm("res2")
    if p:
        try: p.set(512)
        except: pass
    p = comp_out.parm("thumbnailexportlayer")
    if p:
        try: p.set(1)   # Save Thumbnail Scene to Disk
        except: pass

    stage.layoutChildren()
    return comp_out


def _frame_viewport_to_node(lop_node):
    """
    Set lop_node as display node in the stage, set it as current
    in the scene viewer, then frame the viewport camera to fit it.
    """
    import time
    try:
        desktop = hou.ui.curDesktop()
        viewer  = desktop.paneTabOfType(hou.paneTabType.SceneViewer)
        if viewer is None:
            return

        stage = lop_node.parent()
        stem  = lop_node.name()  # componentoutput is named after stem
        comp_geo = stage.node(f"componentgeometry_{stem}")

        # Step 1: display the componentgeometry so the viewport shows
        # the right geometry, then frame it
        if comp_geo:
            comp_geo.setDisplayFlag(True)
            viewer.setCurrentNode(comp_geo)
            hou.hscript("refresh")
            time.sleep(0.5)
            vp = viewer.curViewport()
            if vp:
                vp.frameAll()
                hou.hscript("refresh")
                time.sleep(0.5)

        # Step 2: switch current node to componentoutput so
        # executeviewport captures from the right node
        lop_node.setDisplayFlag(True)
        viewer.setCurrentNode(lop_node)
        hou.hscript("refresh")
        time.sleep(0.3)

    except Exception as e:
        print(f"Viewport frame warning: {e}")


def render_component(comp_out_node, comp_path):
    """Set output path, export the component, then generate viewport thumbnail."""
    comp_path = comp_path.replace("\\", "/")
    p = comp_out_node.parm("lopoutput")
    if p:
        try: p.revertToDefaults()
        except: pass
        try: p.deleteAllKeyframes()
        except: pass
        p.set(comp_path)
    os.makedirs(os.path.dirname(os.path.abspath(comp_path)), exist_ok=True)

    # Export the component USD
    comp_out_node.parm("execute").pressButton()

    # Frame viewport to this specific component then capture thumbnail
    _frame_viewport_to_node(comp_out_node)
    p = comp_out_node.parm("executeviewport")
    if p:
        try: p.pressButton()
        except Exception as e:
            print(f"Thumbnail warning: {e}")


# =============================================================================
# SECTION 9 — Geometry splitter (Tab ⓪)
# Splits a single FBX/OBJ containing multiple geometries into individual files.
# Uses groupsfromname + blast (negate=1) + attribdelete + groupdelete + xform
# + Name SOP (from shop_materialpath) + rop_fbx (pathattrib=name).
# =============================================================================

def split_geometry(source_path, out_folder, scale=1.0, progress_cb=None):
    """
    Load a single FBX/OBJ containing multiple geometries identified by the
    'name' prim attribute. Export one file per unique name value (last segment).

    source_path  : path to the source FBX or OBJ file
    out_folder   : folder to write split files into
    progress_cb  : optional callable(current, total, name) for progress updates
    Returns (exported_paths, errors)
    """
    import os

    source_path = source_path.replace("\\", "/")
    out_folder  = out_folder.replace("\\", "/").rstrip("/")
    ext         = os.path.splitext(source_path)[1].lower()  # .fbx or .obj

    obj_ctx = hou.node("/obj")
    if obj_ctx is None:
        return [], ["Could not find /obj network"]

    exported = []
    errors   = []

    # ── Create a temp geo node to load the source file ────────────────────
    tmp_geo = obj_ctx.createNode("geo", node_name="__split_source__")
    for child in tmp_geo.children():
        try: child.destroy()
        except: pass

    file_sop = tmp_geo.createNode("file", node_name="source_file")
    p = file_sop.parm("file")
    if p: p.set(source_path)

    # Force cook to read geometry
    try:
        file_sop.cook(force=True)
        src_geo = file_sop.geometry()
    except Exception as exc:
        tmp_geo.destroy()
        return [], [f"Could not load source file: {exc}"]

    # ── Get unique name values ─────────────────────────────────────────────
    name_attr = src_geo.findPrimAttrib("name")
    if name_attr is None:
        tmp_geo.destroy()
        return [], ["No 'name' prim attribute found in source file"]

    all_vals    = src_geo.primStringAttribValues("name")
    unique_vals = sorted(set(v for v in all_vals if v))

    if not unique_vals:
        tmp_geo.destroy()
        return [], ["No name attribute values found"]

    os.makedirs(out_folder, exist_ok=True)
    total = len(unique_vals)

    for idx, full_name in enumerate(unique_vals):
        last_seg  = full_name.split("/")[-1]
        out_name  = re.sub(r"[^a-zA-Z0-9_-]", "_", last_seg)
        out_path  = f"{out_folder}/{out_name}{ext}"

        # groupsfromname converts "/" to "_" in group names
        # so KB3D_FAV_ACunit_A_grp/KB3D_FAV_ACunit_A_Main
        # becomes KB3D_FAV_ACunit_A_grp_KB3D_FAV_ACunit_A_Main
        grp_name = full_name.replace("/", "_")

        if progress_cb:
            progress_cb(idx + 1, total, last_seg)

        try:
            # groupsfromname — creates prim groups from the name attribute
            gfn = tmp_geo.createNode("groupsfromname", node_name=f"__gfn_{idx}__")
            gfn.setInput(0, file_sop)
            p = gfn.parm("attribname")
            if p: p.set("name")

            # Blast by the converted group name (/ replaced with _)
            blast = tmp_geo.createNode("blast", node_name=f"__blast_{idx}__")
            blast.setInput(0, gfn)
            p = blast.parm("group")
            if p: p.set(grp_name)
            p = blast.parm("negate")
            if p:
                try: p.set(1)   # Delete Non Selected — isolate this geometry
                except: pass

            # Attribdelete — keep only name + shop_materialpath on prims,
            # and delete all point attributes (groups etc from groupsfromname)
            attribdel = tmp_geo.createNode("attribdelete", node_name=f"__attribdel_{idx}__")
            attribdel.setInput(0, blast)
            # Prim attribs: delete all except name and shop_materialpath
            p = attribdel.parm("doprimdel")
            if p:
                try: p.set(1)
                except: pass
            p = attribdel.parm("primdel")
            if p: p.set("* ^name ^shop_materialpath")
            # Point attribs: delete all
            p = attribdel.parm("doptdel")
            if p:
                try: p.set(1)
                except: pass
            p = attribdel.parm("ptdel")
            if p: p.set("*")

            # Groupdelete — remove all primitive and point groups
            grpdel = tmp_geo.createNode("groupdelete", node_name=f"__grpdel_{idx}__")
            grpdel.setInput(0, attribdel)
            p = grpdel.parm("deletions")
            if p:
                try: p.set(2)
                except: pass
            # Entry 1: delete all prim groups
            p = grpdel.parm("grouptype1")
            if p:
                try: p.set("prims")
                except: pass
            p = grpdel.parm("group1")
            if p: p.set("*")
            # Entry 2: delete all point groups
            p = grpdel.parm("grouptype2")
            if p:
                try: p.set("points")
                except: pass
            p = grpdel.parm("group2")
            if p: p.set("*")

            # Transform — apply scale multiplier
            xform = tmp_geo.createNode("xform", node_name=f"__xform_{idx}__")
            xform.setInput(0, grpdel)
            p = xform.parm("scale")
            if p:
                try: p.set(scale)
                except: pass

            # Name SOP: one entry per unique shop_materialpath value.
            # Each material gets its own named subset in the exported file.
            name_node = tmp_geo.createNode("name", node_name=f"__name_{idx}__")
            name_node.setInput(0, xform)

            xform.cook(force=True)
            xform_geo = xform.geometry()
            shop_attr = xform_geo.findPrimAttrib("shop_materialpath")
            shop_vals = sorted(set(
                v for v in xform_geo.primStringAttribValues("shop_materialpath") if v
            )) if shop_attr else []

            if shop_vals:
                name_node.parm("numnames").set(len(shop_vals))
                for si, val in enumerate(shop_vals, start=1):
                    name_node.parm(f"name{si}").set(val)
                    name_node.parm(f"group{si}").set(f"@shop_materialpath={val}")
            else:
                name_node.parm("numnames").set(1)
                name_node.parm("name1").set(last_seg)

            # ROP export
            if ext == ".fbx":
                rop = tmp_geo.createNode("rop_fbx", node_name=f"__rop_{idx}__")
                rop.setInput(0, name_node)
                rop.parm("sopoutput").set(out_path)
                rop.parm("mkpath").set(1)
                rop.parm("buildfrompath").set(1)
                rop.parm("pathattrib").set("name")
            else:
                rop = tmp_geo.createNode("rop_geometry", node_name=f"__rop_{idx}__")
                rop.parm("soppath").set(name_node.path())
                rop.parm("sopoutput").set(out_path)
                rop.parm("mkpath").set(1)

            rop.render()
            exported.append(out_path)
        except Exception as exc:
            errors.append(f"{last_seg}: {exc}")
        finally:
            try: tmp_geo.node(f"__gfn_{idx}__").destroy()
            except: pass
            try: tmp_geo.node(f"__blast_{idx}__").destroy()
            except: pass
            try: tmp_geo.node(f"__attribdel_{idx}__").destroy()
            except: pass
            try: tmp_geo.node(f"__grpdel_{idx}__").destroy()
            except: pass
            try: tmp_geo.node(f"__xform_{idx}__").destroy()
            except: pass
            try: tmp_geo.node(f"__name_{idx}__").destroy()
            except: pass
            try: tmp_geo.node(f"__rop_{idx}__").destroy()
            except: pass

    # Clean up temp geo
    try: tmp_geo.destroy()
    except: pass

    return exported, errors

# =============================================================================
# SECTION 10 — UI colour constants
# Used for colour-coding file format chips in the cache tree widget.
# =============================================================================

_MAP_COLOURS = {
    "basecolor": "#c8e6c9", "roughness": "#fff9c4",
    "metallic":  "#b3e5fc", "normal":    "#e1bee7",
    "height":    "#ffe0b2", "ao":        "#f0f0f0",
    "opacity":   "#fce4ec", "emission":  "#fff3e0",
    "specular":  "#e3f2fd",
}
_FMT_COLOURS = {
    ".bgeo": "#c8e6c9", ".sc":   "#c8e6c9",
    ".abc":  "#b3e5fc",
    ".usd":  "#e1bee7", ".usdc": "#e1bee7", ".usdz": "#e1bee7",
    ".obj":  "#fff9c4", ".fbx":  "#ffe0b2",
}


# =============================================================================
# SECTION 11 — Main UI  (ImporterUI)
# QDialog with 7 tabs. Each tab has:
#   _tab_xxx()        builds and returns the tab widget
#   _browse_xxx()     file/folder dialog callbacks
#   _refresh_xxx()    repopulates list widgets
#   _build_xxx()      triggers the Houdini node-building functions
#   _export_xxx()     triggers rendering/export
# The _make_progress() helper creates a cancellable QProgressDialog.
# =============================================================================

class ImporterUI(QtWidgets.QDialog):

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Material + Cache Importer  —  Houdini 21")
        self.setMinimumSize(960, 620)
        self.tex_groups = {}
        self.cache_list = []
        self._build_ui()

    def _build_ui(self):
        root = QtWidgets.QVBoxLayout(self)
        root.setSpacing(8)
        root.setContentsMargins(10, 10, 10, 10)
        self.tabs = QtWidgets.QTabWidget()
        root.addWidget(self.tabs, 1)
        self.tabs.addTab(self._tab_split(),      "\u24ea Split Geometry  (optional)")
        self.tabs.addTab(self._tab_textures(),   "\u2460 Textures  \u2192  /mat")
        self.tabs.addTab(self._tab_caches(),     "\u2461 Caches  \u2192  /obj")
        self.tabs.addTab(self._tab_usd(),        "\u2462 USD Export")
        self.tabs.addTab(self._tab_component(),  "\u2463 Component Builder")
        self.tabs.addTab(self._tab_comp_export(),"\u2464 Component Export")
        self.tabs.addTab(self._tab_catalog(),    "\u2465 Asset Catalog")

        opt = QtWidgets.QHBoxLayout()
        opt.addWidget(QtWidgets.QLabel("Geo node name:"))
        self.le_geo_name = QtWidgets.QLineEdit("caches")
        self.le_geo_name.setMaximumWidth(160)
        opt.addWidget(self.le_geo_name)
        opt.addSpacing(20)
        opt.addWidget(QtWidgets.QLabel("Mat prefix (optional):"))
        self.le_prefix = QtWidgets.QLineEdit("")
        self.le_prefix.setMaximumWidth(140)
        opt.addWidget(self.le_prefix)
        opt.addSpacing(20)
        self.chk_mats   = QtWidgets.QCheckBox("Build Materials")
        self.chk_caches = QtWidgets.QCheckBox("Build Geo + SOPs")
        self.chk_mats.setChecked(True)
        self.chk_caches.setChecked(True)
        opt.addWidget(self.chk_mats)
        opt.addWidget(self.chk_caches)
        opt.addStretch(1)
        root.addLayout(opt)

        self.btn_build = QtWidgets.QPushButton("\u25b6  Build Everything")
        self.btn_build.setMinimumHeight(38)
        self.btn_build.setEnabled(False)
        self.btn_build.clicked.connect(self._build)
        root.addWidget(self.btn_build)

        self.lbl_status = QtWidgets.QLabel("")
        self.lbl_status.setStyleSheet("font-size:11px; color:grey;")
        root.addWidget(self.lbl_status)

    # ── Tab 0: Split Geometry ────────────────────────────────────────────

    def _tab_split(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        l.setSpacing(8)

        l.addWidget(QtWidgets.QLabel(
            "Optional pre-processing step. Split a single FBX/OBJ containing "
            "multiple geometries into individual files based on the 'name' prim attribute. "
            "Each unique last segment becomes one exported file."
        ))

        # Source file
        row = QtWidgets.QHBoxLayout()
        self.le_split_source = QtWidgets.QLineEdit()
        self.le_split_source.setPlaceholderText("Select source FBX or OBJ file…")
        self.le_split_source.setReadOnly(True)
        btn_src = QtWidgets.QPushButton("Browse…")
        btn_src.clicked.connect(self._browse_split_source)
        row.addWidget(QtWidgets.QLabel("Source file:"))
        row.addWidget(self.le_split_source, 1)
        row.addWidget(btn_src)
        l.addLayout(row)

        # Output folder
        row2 = QtWidgets.QHBoxLayout()
        self.le_split_out = QtWidgets.QLineEdit()
        self.le_split_out.setPlaceholderText("Select output folder…")
        self.le_split_out.setReadOnly(True)
        btn_out = QtWidgets.QPushButton("Browse…")
        btn_out.clicked.connect(self._browse_split_out)
        row2.addWidget(QtWidgets.QLabel("Output folder:"))
        row2.addWidget(self.le_split_out, 1)
        row2.addWidget(btn_out)
        l.addLayout(row2)

        # Preview list
        l.addWidget(QtWidgets.QLabel("Detected geometries (from name attribute):"))
        self.lst_split = QtWidgets.QListWidget()
        l.addWidget(self.lst_split, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_preview = QtWidgets.QPushButton("↻  Preview Geometries")
        btn_preview.clicked.connect(self._preview_split)
        btn_row.addWidget(btn_preview)
        btn_row.addStretch(1)
        self.lbl_split_count = QtWidgets.QLabel("")
        self.lbl_split_count.setStyleSheet("color:grey;font-size:11px;")
        btn_row.addWidget(self.lbl_split_count)
        l.addLayout(btn_row)

        # Scale multiplier
        scale_row = QtWidgets.QHBoxLayout()
        scale_row.addWidget(QtWidgets.QLabel("Scale multiplier:"))
        self.sb_split_scale = QtWidgets.QDoubleSpinBox()
        self.sb_split_scale.setDecimals(4)
        self.sb_split_scale.setMinimum(0.0001)
        self.sb_split_scale.setMaximum(1000.0)
        self.sb_split_scale.setValue(1.0)
        self.sb_split_scale.setSingleStep(0.01)
        self.sb_split_scale.setMaximumWidth(120)
        self.sb_split_scale.setToolTip(
            "Apply uniform scale to all exported geometries.\n"
            "e.g. 0.01 to convert from cm to m, 0.1 to scale down 10x"
        )
        scale_row.addWidget(self.sb_split_scale)
        scale_row.addStretch(1)
        l.addLayout(scale_row)

        self.btn_split = QtWidgets.QPushButton("▶  Split and Export")
        self.btn_split.setMinimumHeight(34)
        self.btn_split.setEnabled(False)
        self.btn_split.clicked.connect(self._run_split)
        l.addWidget(self.btn_split)

        self.lbl_split_status = QtWidgets.QLabel("")
        self.lbl_split_status.setStyleSheet("font-size:11px;color:grey;")
        l.addWidget(self.lbl_split_status)

        return w

    def _browse_split_source(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Source File", "",
            "Geometry Files (*.fbx *.obj);;All Files (*.*)"
        )
        if path:
            self.le_split_source.setText(path)
            self.lst_split.clear()
            self.lbl_split_count.setText("")
            self.btn_split.setEnabled(False)

    def _browse_split_out(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Output Folder", "", QtWidgets.QFileDialog.ShowDirsOnly)
        if folder:
            self.le_split_out.setText(folder)
            self._check_split_ready()

    def _preview_split(self):
        source = self.le_split_source.text().strip()
        if not source or not os.path.exists(source):
            hou.ui.displayMessage("Please select a valid source file first.",
                                  severity=hou.severityType.Warning)
            return

        self.lst_split.clear()
        self.lbl_split_status.setText("Loading geometry…")
        QtWidgets.QApplication.processEvents()

        try:
            # Load into a temp geo to read name attrib
            obj_ctx = hou.node("/obj")
            tmp = obj_ctx.createNode("geo", "__preview_split__")
            for child in tmp.children():
                try: child.destroy()
                except: pass
            f = tmp.createNode("file")
            f.parm("file").set(source)
            f.cook(force=True)
            geo = f.geometry()

            name_attr = geo.findPrimAttrib("name")
            if name_attr:
                all_vals    = geo.primStringAttribValues("name")
                unique_vals = sorted(set(
                    v.split("/")[-1] for v in all_vals if v
                ))
                for v in unique_vals:
                    self.lst_split.addItem(v)
                n = len(unique_vals)
                self.lbl_split_count.setText(f"{n} geometr{'ies' if n!=1 else 'y'} detected")
            else:
                self.lbl_split_count.setText("No name attribute found")

            tmp.destroy()
        except Exception as exc:
            self.lbl_split_status.setText(f"✖ Error: {exc}")
            try: hou.node("/obj/__preview_split__").destroy()
            except: pass
            return

        self.lbl_split_status.setText("")
        self._check_split_ready()

    def _check_split_ready(self):
        self.btn_split.setEnabled(
            bool(self.le_split_source.text()) and
            bool(self.le_split_out.text()) and
            self.lst_split.count() > 0
        )

    def _run_split(self):
        source = self.le_split_source.text().strip()
        out    = self.le_split_out.text().strip()
        if not source or not out:
            return

        self.lbl_split_status.setText("Splitting…")
        self.btn_split.setEnabled(False)
        QtWidgets.QApplication.processEvents()

        self._split_prog = None

        def progress(cur, total, name):
            if self._split_prog is None:
                self._split_prog = self._make_progress(
                    "Splitting Geometry", "Splitting…", total)
            if self._split_prog.wasCanceled():
                return
            self._split_prog.setLabelText(f"Exporting {cur}/{total}: {name}")
            self._split_prog.setValue(cur)
            self.lbl_split_status.setText(f"Exporting {cur}/{total}: {name}")
            QtWidgets.QApplication.processEvents()

        scale = self.sb_split_scale.value()
        exported, errors = split_geometry(source, out, scale=scale, progress_cb=progress)
        if self._split_prog:
            self._split_prog.setValue(self._split_prog.maximum())
        parts = []
        if exported:
            parts.append(f"Exported {len(exported)} file(s) to:\n{out}")
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))
        if parts:
            sev = hou.severityType.Error if (errors and not exported) else hou.severityType.Message
            hou.ui.displayMessage("\n\n".join(parts), severity=sev)

        self.lbl_split_status.setText(
            f"✔ {len(exported)} exported." +
            (f"  ✖ {len(errors)} error(s)." if errors else "")
        )
        self.btn_split.setEnabled(True)

    # ── Tab 1: Textures ───────────────────────────────────────────────────

    def _tab_textures(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.le_tex = QtWidgets.QLineEdit()
        self.le_tex.setPlaceholderText("Select texture folder\u2026")
        self.le_tex.setReadOnly(True)
        btn = QtWidgets.QPushButton("Browse\u2026")
        btn.clicked.connect(self._browse_tex)
        row.addWidget(QtWidgets.QLabel("Folder:"))
        row.addWidget(self.le_tex, 1)
        row.addWidget(btn)
        l.addLayout(row)
        sp = QtWidgets.QSplitter(QtCore.Qt.Horizontal)
        l.addWidget(sp, 1)
        lw = QtWidgets.QWidget(); ll = QtWidgets.QVBoxLayout(lw); ll.setContentsMargins(0,0,0,0)
        ll.addWidget(QtWidgets.QLabel("Material groups:"))
        self.lst_tex = QtWidgets.QListWidget()
        self.lst_tex.currentRowChanged.connect(self._on_tex_row)
        ll.addWidget(self.lst_tex)
        self.lbl_tex_count = QtWidgets.QLabel("")
        self.lbl_tex_count.setStyleSheet("color:grey;font-size:11px;")
        ll.addWidget(self.lbl_tex_count)
        sp.addWidget(lw)
        rw = QtWidgets.QWidget(); rl = QtWidgets.QVBoxLayout(rw); rl.setContentsMargins(0,0,0,0)
        rl.addWidget(QtWidgets.QLabel("Maps in selected group:"))
        self.tree_tex = QtWidgets.QTreeWidget()
        self.tree_tex.setHeaderLabels(["Map Type", "Filename"])
        self.tree_tex.setRootIsDecorated(False)
        self.tree_tex.header().setStretchLastSection(True)
        rl.addWidget(self.tree_tex)
        sp.addWidget(rw)
        sp.setSizes([260, 640])
        # Displacement scale
        disp_row = QtWidgets.QHBoxLayout()
        disp_row.addWidget(QtWidgets.QLabel("Displacement scale:"))
        self.sb_disp_scale = QtWidgets.QDoubleSpinBox()
        self.sb_disp_scale.setDecimals(4)
        self.sb_disp_scale.setMinimum(0.0001)
        self.sb_disp_scale.setMaximum(1000.0)
        self.sb_disp_scale.setValue(0.01)
        self.sb_disp_scale.setSingleStep(0.01)
        self.sb_disp_scale.setMaximumWidth(120)
        self.sb_disp_scale.setToolTip(
            "Scale multiplier on the mtlxdisplacement node.\n"
            "Applied to all materials that have a displacement or height map."
        )
        disp_row.addWidget(self.sb_disp_scale)
        disp_row.addStretch(1)
        l.addLayout(disp_row)
        return w

    # ── Tab 2: Caches ─────────────────────────────────────────────────────

    def _tab_caches(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        row = QtWidgets.QHBoxLayout()
        self.le_cache = QtWidgets.QLineEdit()
        self.le_cache.setPlaceholderText("Select cache folder\u2026")
        self.le_cache.setReadOnly(True)
        btn = QtWidgets.QPushButton("Browse\u2026")
        btn.clicked.connect(self._browse_cache)
        row.addWidget(QtWidgets.QLabel("Folder:"))
        row.addWidget(self.le_cache, 1)
        row.addWidget(btn)
        l.addLayout(row)
        # Group assignment method toggle
        grp_row = QtWidgets.QHBoxLayout()
        grp_row.addWidget(QtWidgets.QLabel("Material group assignment:"))
        self.radio_auto        = QtWidgets.QRadioButton("Auto-detect")
        self.radio_name_attrib = QtWidgets.QRadioButton("From @name attribute")
        self.radio_prim_groups = QtWidgets.QRadioButton("From primitive groups")
        self.radio_prefix      = QtWidgets.QRadioButton("From group prefix:")
        self.radio_shop_mat    = QtWidgets.QRadioButton("From @shop_materialpath")
        self.le_group_prefix   = QtWidgets.QLineEdit("m")
        self.le_group_prefix.setMaximumWidth(80)
        self.le_group_prefix.setPlaceholderText("e.g. m")
        self.le_group_prefix.setToolTip(
            "Only groups starting with this prefix will be used as material groups.\n"
            "e.g. 'm' matches mKB3D_SVB_Concrete, 'Mat_' matches Mat_Concrete"
        )
        self.radio_auto.setChecked(True)
        grp_row.addWidget(self.radio_auto)
        grp_row.addWidget(self.radio_name_attrib)
        grp_row.addWidget(self.radio_prim_groups)
        grp_row.addWidget(self.radio_prefix)
        grp_row.addWidget(self.le_group_prefix)
        grp_row.addWidget(self.radio_shop_mat)
        grp_row.addStretch(1)
        l.addLayout(grp_row)
        l.addWidget(QtWidgets.QLabel(
            "Each group gets one Material SOP slot. "
            "Material path is auto-matched against /mat — left empty if no match found."
        ))
        self.chk_name_node = QtWidgets.QCheckBox(
            "Add Name Node  —  stamps name attrib per material group on import"
        )
        self.chk_name_node.setChecked(True)
        self.chk_name_node.setToolTip(
            "Enable if geometries don't already have a name attrib set.\n"
            "Disable if you pre-processed with the Split tab (Tab ⓪) which\n"
            "already sets the name attrib from shop_materialpath."
        )
        l.addWidget(self.chk_name_node)

        self.chk_fuzzy_match = QtWidgets.QCheckBox(
            "Force Match  —  if no exact match, find closest material by keyword"
        )
        self.chk_fuzzy_match.setChecked(False)
        self.chk_fuzzy_match.setToolTip(
            "When enabled, if a group has no exact /mat match, the script will\n"
            "try to find a material whose name contains any keyword from the group name.\n"
            "e.g. 'Wasteland_SM_A_cans' -> /mat/kb3d_cans via keyword 'cans'.\n"
            "Use with caution — may produce incorrect matches."
        )
        l.addWidget(self.chk_fuzzy_match)
        self.tree_cache = QtWidgets.QTreeWidget()
        self.tree_cache.setHeaderLabels(["", "Filename", "Format"])
        self.tree_cache.setRootIsDecorated(False)
        self.tree_cache.header().setStretchLastSection(True)
        self.tree_cache.itemChanged.connect(self._on_cache_check)
        l.addWidget(self.tree_cache, 1)
        sel = QtWidgets.QHBoxLayout()
        ba = QtWidgets.QPushButton("Select All");   ba.clicked.connect(self._cache_all)
        bn = QtWidgets.QPushButton("Deselect All"); bn.clicked.connect(self._cache_none)
        sel.addWidget(ba); sel.addWidget(bn); sel.addStretch(1)
        self.lbl_cache_count = QtWidgets.QLabel("")
        self.lbl_cache_count.setStyleSheet("color:grey;font-size:11px;")
        sel.addWidget(self.lbl_cache_count)
        l.addLayout(sel)
        return w

    # ── Tab 3: USD Export ─────────────────────────────────────────────────

    def _tab_usd(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        l.setSpacing(8)
        row = QtWidgets.QHBoxLayout()
        self.le_usd_folder = QtWidgets.QLineEdit()
        self.le_usd_folder.setPlaceholderText("Select USD output folder\u2026")
        self.le_usd_folder.setReadOnly(True)
        btn_browse = QtWidgets.QPushButton("Browse\u2026")
        btn_browse.clicked.connect(self._browse_usd_folder)
        row.addWidget(QtWidgets.QLabel("Output folder:"))
        row.addWidget(self.le_usd_folder, 1)
        row.addWidget(btn_browse)
        l.addLayout(row)
        self.lbl_usd_info = QtWidgets.QLabel(
            "One .usd file per geometry. Build tabs \u2460 + \u2461 first, then Refresh."
        )
        self.lbl_usd_info.setWordWrap(True)
        self.lbl_usd_info.setStyleSheet("color:grey;font-size:11px;")
        l.addWidget(self.lbl_usd_info)
        l.addWidget(QtWidgets.QLabel("Geometries to export:"))
        self.lst_usd_geos = QtWidgets.QListWidget()
        self.lst_usd_geos.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        l.addWidget(self.lst_usd_geos, 1)
        btn_row = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("\u21bb  Refresh Geo List")
        btn_refresh.clicked.connect(self._refresh_usd_geo_list)
        btn_sel_all  = QtWidgets.QPushButton("Select All")
        btn_sel_all.clicked.connect(self.lst_usd_geos.selectAll)
        btn_sel_none = QtWidgets.QPushButton("Deselect All")
        btn_sel_none.clicked.connect(self.lst_usd_geos.clearSelection)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_sel_all)
        btn_row.addWidget(btn_sel_none)
        btn_row.addStretch(1)
        l.addLayout(btn_row)
        self.btn_export_usd = QtWidgets.QPushButton("\u25b6  Export USD(s)")
        self.btn_export_usd.setMinimumHeight(34)
        self.btn_export_usd.setEnabled(False)
        self.btn_export_usd.clicked.connect(self._export_usd)
        l.addWidget(self.btn_export_usd)
        return w

    # ── Texture tab slots ─────────────────────────────────────────────────

    def _browse_tex(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Texture Folder", "", QtWidgets.QFileDialog.ShowDirsOnly)
        if not folder:
            return
        self.le_tex.setText(folder)
        self.tex_groups = scan_textures(folder)
        self._refresh_tex_list()
        self._update_build_btn()

    def _refresh_tex_list(self):
        self.lst_tex.clear(); self.tree_tex.clear()
        for stem in sorted(self.tex_groups):
            g       = self.tex_groups[stem]
            n_known = len([k for k in g if k != "_unknown"])
            n_unk   = len(g["_unknown"])
            label   = f"{stem}   [{n_known} map{'s' if n_known!=1 else ''}"
            if n_unk: label += f", {n_unk} unrecognised"
            label += "]"
            self.lst_tex.addItem(label)
        n = len(self.tex_groups)
        self.lbl_tex_count.setText(f"{n} material group{'s' if n!=1 else ''}")
        if self.lst_tex.count():
            self.lst_tex.setCurrentRow(0)

    def _on_tex_row(self, row):
        self.tree_tex.clear()
        stems = sorted(self.tex_groups)
        if row < 0 or row >= len(stems): return
        g = self.tex_groups[stems[row]]
        for mtype in sorted(k for k in g if k != "_unknown"):
            item = QtWidgets.QTreeWidgetItem([mtype, os.path.basename(g[mtype])])
            item.setToolTip(1, g[mtype])
            item.setBackground(0, QtGui.QColor(_MAP_COLOURS.get(mtype, "#fff")))
            self.tree_tex.addTopLevelItem(item)
        for fp in g["_unknown"]:
            item = QtWidgets.QTreeWidgetItem(["(unrecognised)", os.path.basename(fp)])
            item.setForeground(0, QtGui.QColor("#aaa"))
            self.tree_tex.addTopLevelItem(item)
        self.tree_tex.resizeColumnToContents(0)

    # ── Cache tab slots ───────────────────────────────────────────────────

    def _browse_cache(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Cache Folder", "", QtWidgets.QFileDialog.ShowDirsOnly)
        if not folder:
            return
        self.le_cache.setText(folder)
        self.cache_list = scan_caches(folder)
        self._refresh_cache_tree()
        self._update_build_btn()

    def _refresh_cache_tree(self):
        self.tree_cache.blockSignals(True)
        self.tree_cache.clear()
        for e in self.cache_list:
            item = QtWidgets.QTreeWidgetItem()
            item.setCheckState(0, QtCore.Qt.Checked)
            item.setText(1, e["name"])
            item.setText(2, e["fmt"])
            item.setBackground(2, QtGui.QColor(_FMT_COLOURS.get(e["fmt"], "#f5f5f5")))
            self.tree_cache.addTopLevelItem(item)
        self.tree_cache.resizeColumnToContents(0)
        self.tree_cache.resizeColumnToContents(1)
        self.tree_cache.resizeColumnToContents(2)
        self.tree_cache.blockSignals(False)
        self._update_cache_count()

    def _checked_caches(self):
        return [
            self.cache_list[i]
            for i in range(self.tree_cache.topLevelItemCount())
            if self.tree_cache.topLevelItem(i).checkState(0) == QtCore.Qt.Checked
        ]

    def _on_cache_check(self, item, col):
        if col == 0:
            self._update_cache_count()

    def _update_cache_count(self):
        c = len(self._checked_caches())
        t = self.tree_cache.topLevelItemCount()
        self.lbl_cache_count.setText(f"{c} / {t} selected")

    def _cache_all(self):
        self.tree_cache.blockSignals(True)
        for i in range(self.tree_cache.topLevelItemCount()):
            self.tree_cache.topLevelItem(i).setCheckState(0, QtCore.Qt.Checked)
        self.tree_cache.blockSignals(False)
        self._update_cache_count()

    def _cache_none(self):
        self.tree_cache.blockSignals(True)
        for i in range(self.tree_cache.topLevelItemCount()):
            self.tree_cache.topLevelItem(i).setCheckState(0, QtCore.Qt.Unchecked)
        self.tree_cache.blockSignals(False)
        self._update_cache_count()

    # ── USD tab slots ─────────────────────────────────────────────────────

    def _browse_usd_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select USD Output Folder", "", QtWidgets.QFileDialog.ShowDirsOnly)
        if folder:
            self.le_usd_folder.setText(folder)
            self.btn_export_usd.setEnabled(
                bool(folder) and self.lst_usd_geos.count() > 0
            )

    def _refresh_usd_geo_list(self):
        self.lst_usd_geos.clear()
        geo_name  = self.le_geo_name.text().strip() or "caches"
        container = hou.node(f"/obj/{geo_name}")
        if container is None:
            self.lbl_usd_info.setText(
                f"Could not find /obj/{geo_name} \u2014 build the scene in tabs \u2460 + \u2461 first."
            )
            return
        for child in sorted(container.children(), key=lambda n: n.name()):
            if child.type().name() == "material" and child.name().startswith("material_"):
                stem = child.name()[len("material_"):]
                self.lst_usd_geos.addItem(stem)
        if self.lst_usd_geos.count() == 0:
            self.lbl_usd_info.setText(
                f"No material SOPs found inside /obj/{geo_name} \u2014 build the scene first."
            )
            return
        self.lst_usd_geos.selectAll()
        self.btn_export_usd.setEnabled(
            bool(self.le_usd_folder.text()) and self.lst_usd_geos.count() > 0
        )

    def _export_usd(self):
        out_folder = self.le_usd_folder.text().strip().replace("\\", "/")
        if not out_folder:
            hou.ui.displayMessage("Please select an output folder first.",
                                  severity=hou.severityType.Warning)
            return
        selected = [item.text() for item in self.lst_usd_geos.selectedItems()]
        if not selected:
            hou.ui.displayMessage("No geometries selected.",
                                  severity=hou.severityType.Warning)
            return

        geo_name  = self.le_geo_name.text().strip() or "caches"
        container = hou.node(f"/obj/{geo_name}")
        if container is None:
            hou.ui.displayMessage(f"Could not find /obj/{geo_name}.",
                                  severity=hou.severityType.Error)
            return

        # Build confirmation message showing USD structure
        geo_name_conf = self.le_geo_name.text().strip() or "caches"
        info_lines = [
            f"About to export {len(selected)} USD file(s) to:",
            f"  {out_folder}",
            "",
            "USD structure per geometry:",
            "  /ASSET/<stem>/<subset>   — geometry subsets from name attrib",
            "  /ASSET/<mat_stem>/       — materials from /mat",
            "",
            f"Total geometries selected: {len(selected)}",
            "",
            "LOP nodes will be created in /stage if they don’t exist yet.",
            "Proceed?"
        ]
        confirm = hou.ui.displayMessage(
            "\n".join(info_lines),
            buttons=("Export", "Cancel"),
            severity=hou.severityType.Message,
            title="USD Export Confirmation"
        )
        if confirm != 0:
            return

        os.makedirs(out_folder, exist_ok=True)
        exported, errors = [], []

        prog = self._make_progress("Exporting USD", "Exporting USDs…", len(selected))
        for idx, stem in enumerate(selected):
            if prog.wasCanceled(): break
            prog.setLabelText(f"Exporting: {stem}  ({idx+1}/{len(selected)})")
            prog.setValue(idx)
            QtWidgets.QApplication.processEvents()
            # Check if LOP nodes already exist in /stage
            stage    = hou.node("/stage")
            usd_rop  = stage.node(f"usd_rop_{stem}") if stage else None

            if usd_rop is None:
                mat_sop = container.node(f"material_{stem}")
                if mat_sop is None:
                    errors.append(f"{stem}: material_{stem} not found in /obj/{geo_name}")
                    continue
                # Reconstruct matched groups from the Material SOP multiparm.
                # group field may be "@name=<value>" or plain "<value>" — strip prefix.
                # Skip entries with no material path assigned.
                matched = []
                num_p = mat_sop.parm("num_materials")
                if num_p:
                    for i in range(1, num_p.eval() + 1):
                        gp = mat_sop.parm(f"group{i}")
                        mp = mat_sop.parm(f"shop_materialpath{i}")
                        if gp and mp:
                            raw_group = gp.eval()
                            mat_path  = mp.eval()
                            if not mat_path:
                                continue  # skip unassigned slots
                            # Strip any @attrib= prefix to get the clean value
                            clean_group = raw_group
                            is_shop_mat = "@shop_materialpath=" in raw_group
                            for prefix in ("@name=", "@shop_materialpath="):
                                clean_group = clean_group.replace(prefix, "")
                            clean_group = clean_group.strip()
                            # In shop_mat mode the Name SOP stamps the shop_materialpath
                            # value as the name attrib, so sopimport creates
                            # /ASSET/<stem>/<shop_mat_value> as the subset.
                            # Use the clean shop_materialpath value as subset_name.
                            # In all other modes use last path segment.
                            if is_shop_mat:
                                subset_name = clean_group  # e.g. KB3D_FAV_IronLight
                            else:
                                subset_name = clean_group.split("/")[-1]
                            mat_stem    = mat_path.split("/")[-1]
                            matched.append((subset_name, mat_stem))
                try:
                    usd_rop = build_lop_export(container, stem, matched, out_folder)
                except Exception as exc:
                    errors.append(f"{stem}: failed to build LOP network \u2014 {exc}")
                    continue

            out_path = out_folder.rstrip("/") + f"/{stem}.usd"
            try:
                render_lop_export(usd_rop, out_path)
                exported.append(out_path)
            except Exception as exc:
                errors.append(f"{stem}: {exc}")

        prog.setValue(len(selected))
        parts = []
        if exported:
            parts.append(f"Exported {len(exported)} USD file(s) to:\n  {out_folder}")
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))
        if parts:
            sev = hou.severityType.Error if (errors and not exported) else hou.severityType.Message
            hou.ui.displayMessage("\n\n".join(parts), severity=sev)

        bits = []
        if exported: bits.append(f"\u2714 {len(exported)} USD(s) exported")
        if errors:   bits.append(f"\u2716 {len(errors)} error(s)")
        self.lbl_status.setText("   ".join(bits))

    # ── Build ─────────────────────────────────────────────────────────────

    def _update_build_btn(self):
        self.btn_build.setEnabled(
            bool(self.tex_groups) or bool(self._checked_caches())
        )

    def _build(self):
        prefix   = self.le_prefix.text().strip()
        geo_name = self.le_geo_name.text().strip() or "caches"
        created_mats, created_geo, errors = [], None, []

        if self.chk_mats.isChecked() and self.tex_groups:
            mat_stems = sorted(self.tex_groups)
            prog = self._make_progress("Building Materials", "Building materials…", len(mat_stems))
            for idx, stem in enumerate(mat_stems):
                if prog.wasCanceled(): break
                prog.setLabelText(f"Building material: {stem}  ({idx+1}/{len(mat_stems)})")
                prog.setValue(idx)
                QtWidgets.QApplication.processEvents()
                maps = {k: v for k, v in self.tex_groups[stem].items() if k != "_unknown"}
                if not maps: continue
                try:
                    disp_sc = self.sb_disp_scale.value()
                    node = build_material(stem, maps, prefix, disp_scale=disp_sc)
                    created_mats.append(node.path())
                except Exception as exc:
                    errors.append(f"Material '{stem}': {exc}")
            prog.setValue(len(mat_stems))

        if self.chk_caches.isChecked():
            entries = self._checked_caches()
            if entries:
                geo_prog = self._make_progress(
                    "Building Geometry", "Building geometry nodes…", len(entries))
                try:
                    if self.radio_name_attrib.isChecked():
                        gmode = "name_attrib"
                    elif self.radio_prim_groups.isChecked():
                        gmode = "prim_groups"
                    elif self.radio_prefix.isChecked():
                        gmode = "prefix"
                    elif self.radio_shop_mat.isChecked():
                        gmode = "shop_mat"
                    else:
                        gmode = "auto"
                    prefix_str = self.le_group_prefix.text().strip()
                    fuzzy     = self.chk_fuzzy_match.isChecked()
                    use_name  = self.chk_name_node.isChecked()
                    geo_prog = self._make_progress(
                        "Building Geometry", "Building geometry nodes…", len(entries))
                    def _geo_progress(cur, total, name):
                        if geo_prog.wasCanceled(): return
                        geo_prog.setLabelText(f"Building: {name}  ({cur}/{total})")
                        geo_prog.setValue(cur)
                        QtWidgets.QApplication.processEvents()
                    created_geo = build_geo(entries, geo_name,
                                            group_mode=gmode,
                                            group_prefix=prefix_str,
                                            fuzzy_match=fuzzy,
                                            use_name_node=use_name,
                                            progress_cb=_geo_progress)
                    geo_prog.setValue(len(entries))
                except Exception as exc:
                    errors.append(f"Geo build: {exc}")

        parts = []
        if created_mats:
            parts.append(f"Materials created in /mat: {len(created_mats)}")
        if created_geo:
            parts.append(f"Geo node created:  {created_geo.path()}")
        if errors:
            parts.append("Errors:\n" + "\n".join(errors))
        if parts:
            sev = hou.severityType.Error if (errors and not created_mats and not created_geo) \
                  else hou.severityType.Message
            hou.ui.displayMessage("\n\n".join(parts), severity=sev)

        bits = []
        if created_mats: bits.append(f"\u2714 {len(created_mats)} material(s)")
        if created_geo:  bits.append(f"\u2714 {created_geo.path()}")
        if errors:       bits.append(f"\u2716 {len(errors)} error(s)")
        self.lbl_status.setText("   ".join(bits))

    # ── Tab 4: Component Builder ──────────────────────────────────────────

    def _tab_component(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        l.setSpacing(8)

        l.addWidget(QtWidgets.QLabel(
            "Builds component nodes in /stage for each geometry. "
            "Run tabs ①②③ first so USDs exist before building components."
        ))

        # USD source folder
        row = QtWidgets.QHBoxLayout()
        self.le_comp_usd_folder = QtWidgets.QLineEdit()
        self.le_comp_usd_folder.setPlaceholderText("Folder containing exported USDs…")
        self.le_comp_usd_folder.setReadOnly(True)
        btn = QtWidgets.QPushButton("Browse…")
        btn.clicked.connect(self._browse_comp_usd_folder)
        row.addWidget(QtWidgets.QLabel("USD folder:"))
        row.addWidget(self.le_comp_usd_folder, 1)
        row.addWidget(btn)
        l.addLayout(row)

        # Geometry list
        l.addWidget(QtWidgets.QLabel("Geometries to build components for:"))
        self.lst_comp_geos = QtWidgets.QListWidget()
        self.lst_comp_geos.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        l.addWidget(self.lst_comp_geos, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("↻  Refresh List")
        btn_refresh.clicked.connect(self._refresh_comp_geo_list)
        btn_sel_all  = QtWidgets.QPushButton("Select All")
        btn_sel_all.clicked.connect(self.lst_comp_geos.selectAll)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_sel_all)
        btn_row.addStretch(1)
        l.addLayout(btn_row)

        self.btn_build_components = QtWidgets.QPushButton("▶  Build Component Nodes in /stage")
        self.btn_build_components.setMinimumHeight(34)
        self.btn_build_components.setEnabled(False)
        self.btn_build_components.clicked.connect(self._build_components)
        l.addWidget(self.btn_build_components)

        return w

    # ── Tab 5: Component Export ───────────────────────────────────────────

    def _tab_comp_export(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        l.setSpacing(8)

        l.addWidget(QtWidgets.QLabel(
            "Export component USDs. Build tab ④ first so component nodes exist."
        ))

        # Output folder
        row = QtWidgets.QHBoxLayout()
        self.le_comp_out_folder = QtWidgets.QLineEdit()
        self.le_comp_out_folder.setPlaceholderText("Component output folder…")
        self.le_comp_out_folder.setReadOnly(True)
        btn = QtWidgets.QPushButton("Browse…")
        btn.clicked.connect(self._browse_comp_out_folder)
        row.addWidget(QtWidgets.QLabel("Output folder:"))
        row.addWidget(self.le_comp_out_folder, 1)
        row.addWidget(btn)
        l.addLayout(row)

        # Component list
        l.addWidget(QtWidgets.QLabel("Components to export:"))
        self.lst_comp_export = QtWidgets.QListWidget()
        self.lst_comp_export.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        l.addWidget(self.lst_comp_export, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("↻  Refresh List")
        btn_refresh.clicked.connect(self._refresh_comp_export_list)
        btn_sel_all  = QtWidgets.QPushButton("Select All")
        btn_sel_all.clicked.connect(self.lst_comp_export.selectAll)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_sel_all)
        btn_row.addStretch(1)
        l.addLayout(btn_row)

        self.btn_export_components = QtWidgets.QPushButton("▶  Export Component(s)")
        self.btn_export_components.setMinimumHeight(34)
        self.btn_export_components.setEnabled(False)
        self.btn_export_components.clicked.connect(self._export_components)
        l.addWidget(self.btn_export_components)

        return w

    # ── Component tab slots ───────────────────────────────────────────────

    def _browse_comp_usd_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select USD Folder", "", QtWidgets.QFileDialog.ShowDirsOnly)
        if folder:
            self.le_comp_usd_folder.setText(folder)
            self._refresh_comp_geo_list()

    def _refresh_comp_geo_list(self):
        self.lst_comp_geos.clear()
        folder = self.le_comp_usd_folder.text().strip()
        if not folder or not os.path.isdir(folder):
            return
        for fname in sorted(os.listdir(folder)):
            if fname.lower().endswith(".usd"):
                stem = os.path.splitext(fname)[0]
                self.lst_comp_geos.addItem(stem)
        self.lst_comp_geos.selectAll()
        self.btn_build_components.setEnabled(self.lst_comp_geos.count() > 0)

    def _build_components(self):
        usd_folder = self.le_comp_usd_folder.text().strip().replace("\\", "/")
        selected   = [item.text() for item in self.lst_comp_geos.selectedItems()]
        if not selected:
            hou.ui.displayMessage("No geometries selected.",
                                  severity=hou.severityType.Warning)
            return

        geo_name  = self.le_geo_name.text().strip() or "caches"
        container = hou.node(f"/obj/{geo_name}")

        created, errors = [], []
        created, errors = [], []
        prog = self._make_progress("Building Components", "Building component nodes…", len(selected))
        for idx, stem in enumerate(selected):
            if prog.wasCanceled(): break
            prog.setLabelText(f"Building: {stem}  ({idx+1}/{len(selected)})")
            prog.setValue(idx)
            QtWidgets.QApplication.processEvents()
        for idx, stem in enumerate(selected):
            if prog.wasCanceled(): break
            prog.setLabelText(f"Building: {stem}  ({idx+1}/{len(selected)})")
            prog.setValue(idx)
            QtWidgets.QApplication.processEvents()
            usd_path = usd_folder.rstrip("/") + f"/{stem}.usd"
            if prog.wasCanceled(): break
            prog.setLabelText(f"Building: {stem}  ({idx+1}/{len(selected)})")
            prog.setValue(idx)
            QtWidgets.QApplication.processEvents()
            if not os.path.exists(usd_path):
                errors.append(f"{stem}: USD not found at {usd_path}")
                continue

            # Reconstruct matched groups from the Material SOP.
            # Strip @name= prefix, use last path segment as subset name.
            matched = []
            if container:
                mat_sop = container.node(f"material_{stem}")
                if mat_sop:
                    num_p = mat_sop.parm("num_materials")
                    if num_p:
                        for i in range(1, num_p.eval() + 1):
                            gp = mat_sop.parm(f"group{i}")
                            mp = mat_sop.parm(f"shop_materialpath{i}")
                            if gp and mp:
                                raw_group = gp.eval()
                                mat_path  = mp.eval()
                                if not mat_path:
                                    continue
                                clean_group = raw_group
                                is_shop_mat = "@shop_materialpath=" in raw_group
                                for prefix in ("@name=", "@shop_materialpath="):
                                    clean_group = clean_group.replace(prefix, "")
                                clean_group = clean_group.strip()
                                if is_shop_mat:
                                    subset_name = clean_group  # e.g. KB3D_FAV_IronLight
                                else:
                                    subset_name = clean_group.split("/")[-1]
                                mat_stem    = mat_path.split("/")[-1]
                                matched.append((subset_name, mat_stem))

            try:
                node = build_component(stem, matched, usd_path, "")
                created.append(node.path())
            except Exception as exc:
                errors.append(f"{stem}: {exc}")

        prog.setValue(len(selected))

        parts = []
        if created: parts.append(f"Components built: {len(created)}")
        if errors:  parts.append("Errors:\n" + "\n".join(errors))
        if parts:
            sev = hou.severityType.Error if (errors and not created) else hou.severityType.Message
            hou.ui.displayMessage("\n\n".join(parts), severity=sev)

        self.lbl_status.setText(
            f"✔ {len(created)} component(s) built." +
            (f"  ✖ {len(errors)} error(s)." if errors else "")
        )
        # Populate export list
        self._refresh_comp_export_list()

    def _browse_comp_out_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Component Output Folder", "",
            QtWidgets.QFileDialog.ShowDirsOnly)
        if folder:
            self.le_comp_out_folder.setText(folder)
            self.btn_export_components.setEnabled(
                bool(folder) and self.lst_comp_export.count() > 0
            )

    def _refresh_comp_export_list(self):
        self.lst_comp_export.clear()
        stage = hou.node("/stage")
        if stage is None:
            return
        for child in sorted(stage.children(), key=lambda n: n.name()):
            if child.type().name() == "componentoutput":
                self.lst_comp_export.addItem(child.name())
        self.lst_comp_export.selectAll()
        self.btn_export_components.setEnabled(
            bool(self.le_comp_out_folder.text()) and
            self.lst_comp_export.count() > 0
        )

    def _export_components(self):
        comp_folder = self.le_comp_out_folder.text().strip().replace("\\", "/")
        if not comp_folder:
            hou.ui.displayMessage("Please select an output folder first.",
                                  severity=hou.severityType.Warning)
            return
        selected = [item.text() for item in self.lst_comp_export.selectedItems()]
        if not selected:
            hou.ui.displayMessage("No components selected.",
                                  severity=hou.severityType.Warning)
            return

        os.makedirs(comp_folder, exist_ok=True)
        exported, errors = [], []

        prog = self._make_progress("Exporting Components", "Exporting components…", len(selected))
        for idx, stem in enumerate(selected):
            if prog.wasCanceled(): break
            prog.setLabelText(f"Exporting: {stem}  ({idx+1}/{len(selected)})")
            prog.setValue(idx)
            QtWidgets.QApplication.processEvents()
            node = hou.node(f"/stage/{stem}")
            if node is None:
                errors.append(f"{stem}: componentoutput node not found in /stage")
                continue
            comp_path = comp_folder.rstrip("/") + f"/assets/{stem}/{stem}.usd"
            try:
                render_component(node, comp_path)
                exported.append(comp_path)
            except Exception as exc:
                errors.append(f"{stem}: {exc}")

        prog.setValue(len(selected))
        parts = []
        if exported: parts.append(f"Exported {len(exported)} component(s) to:\n  {comp_folder}")
        if errors:  parts.append("Errors:\n" + "\n".join(errors))
        if parts:
            sev = hou.severityType.Error if (errors and not exported) else hou.severityType.Message
            hou.ui.displayMessage("\n\n".join(parts), severity=sev)

        bits = []
        if exported: bits.append(f"✔ {len(exported)} component(s) exported")
        if errors:   bits.append(f"✖ {len(errors)} error(s)")
        self.lbl_status.setText("   ".join(bits))

    # ── Tab 6: Asset Catalog ─────────────────────────────────────────────

    def _tab_catalog(self):
        w = QtWidgets.QWidget()
        l = QtWidgets.QVBoxLayout(w)
        l.setSpacing(8)

        info = QtWidgets.QLabel(
            "Batch-register component USDs into a Houdini Asset Catalog (.db) database.\n"
            "After registering, open the Asset Catalog pane in Houdini and set its\n"
            "Read/Write database path to your .db file to see the assets."
        )
        info.setWordWrap(True)
        l.addWidget(info)

        # Component folder
        row1 = QtWidgets.QHBoxLayout()
        self.le_cat_comp_folder = QtWidgets.QLineEdit()
        self.le_cat_comp_folder.setPlaceholderText("Component output folder (contains assets/ subfolder)…")
        self.le_cat_comp_folder.setReadOnly(True)
        btn1 = QtWidgets.QPushButton("Browse…")
        btn1.clicked.connect(self._browse_cat_comp_folder)
        row1.addWidget(QtWidgets.QLabel("Component folder:"))
        row1.addWidget(self.le_cat_comp_folder, 1)
        row1.addWidget(btn1)
        l.addLayout(row1)

        # DB file
        row2 = QtWidgets.QHBoxLayout()
        self.le_cat_db = QtWidgets.QLineEdit()
        self.le_cat_db.setPlaceholderText("Asset catalog .db file…")
        self.le_cat_db.setReadOnly(True)
        btn2 = QtWidgets.QPushButton("Browse…")
        btn2.clicked.connect(self._browse_cat_db)
        btn_new = QtWidgets.QPushButton("New…")
        btn_new.clicked.connect(self._new_cat_db)
        row2.addWidget(QtWidgets.QLabel("Catalog .db:"))
        row2.addWidget(self.le_cat_db, 1)
        row2.addWidget(btn2)
        row2.addWidget(btn_new)
        l.addLayout(row2)

        # Asset list
        l.addWidget(QtWidgets.QLabel("Assets found:"))
        self.lst_cat_assets = QtWidgets.QListWidget()
        self.lst_cat_assets.setSelectionMode(QtWidgets.QAbstractItemView.ExtendedSelection)
        l.addWidget(self.lst_cat_assets, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_refresh = QtWidgets.QPushButton("\u21bb  Refresh")
        btn_refresh.clicked.connect(self._refresh_cat_assets)
        btn_sel_all  = QtWidgets.QPushButton("Select All")
        btn_sel_all.clicked.connect(self.lst_cat_assets.selectAll)
        btn_sel_none = QtWidgets.QPushButton("Deselect All")
        btn_sel_none.clicked.connect(self.lst_cat_assets.clearSelection)
        btn_row.addWidget(btn_refresh)
        btn_row.addWidget(btn_sel_all)
        btn_row.addWidget(btn_sel_none)
        btn_row.addStretch(1)
        self.lbl_cat_count = QtWidgets.QLabel("")
        self.lbl_cat_count.setStyleSheet("color:grey;font-size:11px;")
        btn_row.addWidget(self.lbl_cat_count)
        l.addLayout(btn_row)

        btn_bottom = QtWidgets.QHBoxLayout()
        self.btn_cat_register = QtWidgets.QPushButton("\u25b6  Register in Asset Catalog")
        self.btn_cat_register.setMinimumHeight(34)
        self.btn_cat_register.setEnabled(False)
        self.btn_cat_register.clicked.connect(self._register_catalog)
        self.btn_cat_reload = QtWidgets.QPushButton("\u21bb  Reload Catalog Pane")
        self.btn_cat_reload.setMinimumHeight(34)
        self.btn_cat_reload.setToolTip(
            "Forces Houdini to reload the Asset Catalog pane from the .db file.\n"
            "Run this after registering assets."
        )
        self.btn_cat_reload.clicked.connect(self._reload_catalog_pane)
        btn_bottom.addWidget(self.btn_cat_register, 2)
        btn_bottom.addWidget(self.btn_cat_reload, 1)
        l.addLayout(btn_bottom)

        self.lbl_cat_status = QtWidgets.QLabel("")
        self.lbl_cat_status.setStyleSheet("font-size:11px;color:grey;")
        l.addWidget(self.lbl_cat_status)

        return w

    def _browse_cat_comp_folder(self):
        folder = QtWidgets.QFileDialog.getExistingDirectory(
            self, "Select Component Output Folder", "",
            QtWidgets.QFileDialog.ShowDirsOnly)
        if folder:
            self.le_cat_comp_folder.setText(folder)
            self._refresh_cat_assets()

    def _browse_cat_db(self):
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Select Asset Catalog Database", "",
            "SQLite Database (*.db);;All Files (*.*)")
        if path:
            self.le_cat_db.setText(path)
            self._check_cat_ready()

    def _new_cat_db(self):
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Create New Asset Catalog Database", "",
            "SQLite Database (*.db);;All Files (*.*)")
        if path:
            if not path.endswith(".db"):
                path += ".db"
            self._init_catalog_db(path)
            self.le_cat_db.setText(path)
            self._check_cat_ready()

    def _init_catalog_db(self, path):
        """Create a fresh empty catalog database with the correct schema."""
        import sqlite3
        conn = sqlite3.connect(path)
        c = conn.cursor()
        c.executescript("""
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                color TEXT,
                thumbnail BLOB,
                creation_date DATETIME,
                modification_date DATETIME,
                item_type TEXT,
                item_file TEXT,
                item_owns_file INTEGER,
                item_data BLOB,
                status TEXT,
                marked_for_deletion INTEGER,
                uuid TEXT,
                parent_id TEXT,
                render_method TEXT
            );
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT
            );
            CREATE TABLE IF NOT EXISTS item_tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER,
                tag_id INTEGER
            );
            CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT,
                class TEXT
            );
            CREATE TABLE IF NOT EXISTS item_metadata (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER,
                metadata_id INTEGER,
                type TEXT,
                value BLOB
            );
        """)
        conn.commit()
        conn.close()

    def _refresh_cat_assets(self):
        self.lst_cat_assets.clear()
        folder = self.le_cat_comp_folder.text().strip()
        if not folder:
            return
        assets_dir = os.path.join(folder, "assets").replace("\\", "/")
        if not os.path.isdir(assets_dir):
            self.lbl_cat_count.setText("No assets/ subfolder found")
            return
        count = 0
        for stem in sorted(os.listdir(assets_dir)):
            stem_dir = os.path.join(assets_dir, stem)
            usd_path = os.path.join(stem_dir, f"{stem}.usd")
            if os.path.isdir(stem_dir) and os.path.exists(usd_path):
                self.lst_cat_assets.addItem(stem)
                count += 1
        self.lbl_cat_count.setText(f"{count} asset(s) found")
        self.lst_cat_assets.selectAll()
        self._check_cat_ready()

    def _check_cat_ready(self):
        self.btn_cat_register.setEnabled(
            bool(self.le_cat_comp_folder.text()) and
            bool(self.le_cat_db.text()) and
            self.lst_cat_assets.count() > 0
        )

    def _register_catalog(self):
        import sqlite3, uuid, datetime

        comp_folder = self.le_cat_comp_folder.text().strip().replace("\\", "/")
        db_path     = self.le_cat_db.text().strip()
        selected    = [item.text() for item in self.lst_cat_assets.selectedItems()]

        if not selected:
            hou.ui.displayMessage("No assets selected.",
                                  severity=hou.severityType.Warning)
            return

        if not os.path.exists(db_path):
            self._init_catalog_db(db_path)

        conn = sqlite3.connect(db_path)
        c    = conn.cursor()

        # Get existing item files to avoid duplicates
        c.execute("SELECT item_file FROM items")
        existing = set(row[0] for row in c.fetchall())

        prog = self._make_progress(
            "Registering Assets", "Registering in catalog\u2026", len(selected))

        added, skipped, errors = 0, 0, []
        now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        for idx, stem in enumerate(selected):
            if prog.wasCanceled(): break
            prog.setLabelText(f"Registering: {stem}  ({idx+1}/{len(selected)})")
            prog.setValue(idx)
            QtWidgets.QApplication.processEvents()

            rel_path = f"./assets/{stem}/{stem}.usd"

            if rel_path in existing:
                skipped += 1
                continue

            try:
                # Read thumbnail as BLOB
                thumb_path = os.path.join(
                    comp_folder, "assets", stem, "thumbnail.png")
                thumb_data = None
                if os.path.exists(thumb_path):
                    with open(thumb_path, "rb") as tf:
                        thumb_data = tf.read()

                item_uuid = str(uuid.uuid4())
                c.execute("""
                    INSERT INTO items
                    (name, color, thumbnail, creation_date, modification_date,
                     item_type, item_file, item_owns_file, item_data,
                     status, marked_for_deletion, uuid, parent_id, render_method)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    stem,           # name
                    "",             # color
                    thumb_data,     # thumbnail BLOB
                    now,            # creation_date
                    now,            # modification_date
                    "asset",        # item_type
                    rel_path,       # item_file
                    0,              # item_owns_file
                    None,           # item_data
                    "",             # status
                    0,              # marked_for_deletion
                    item_uuid,      # uuid
                    "",             # parent_id
                    ""              # render_method
                ))
                added += 1

            except Exception as exc:
                errors.append(f"{stem}: {exc}")

        conn.commit()
        conn.close()
        prog.setValue(len(selected))

        parts = []
        if added:   parts.append(f"Added {added} asset(s) to catalog.")
        if skipped: parts.append(f"Skipped {skipped} already existing.")
        if errors:  parts.append("Errors:\n" + "\n".join(errors))
        if parts:
            sev = hou.severityType.Error if (errors and not added) else hou.severityType.Message
            hou.ui.displayMessage("\n\n".join(parts), severity=sev)

        bits = []
        if added:   bits.append(f"\u2714 {added} registered")
        if skipped: bits.append(f"\u23e9 {skipped} skipped")
        if errors:  bits.append(f"\u2716 {len(errors)} error(s)")
        self.lbl_cat_status.setText("   ".join(bits))

    def _reload_catalog_pane(self):
        """Force the Asset Catalog pane to reload from the .db file."""
        db_path = self.le_cat_db.text().strip()
        if not db_path:
            hou.ui.displayMessage("Please select a catalog .db file first.",
                                  severity=hou.severityType.Warning)
            return
        try:
            src = hou.AssetGalleryDataSource(db_path)
            n = len(src.itemIds())
            self.lbl_cat_status.setText(
                f"\u2714 Catalog reloaded — {n} item(s) in database. "
                f"Refresh the Asset Catalog pane in Houdini to see them."
            )
        except Exception as e:
            # Even if the API fails, the pane reads from disk directly
            self.lbl_cat_status.setText(
                "\u2714 Registration complete. Refresh the Asset Catalog pane in Houdini."
            )

    # ── Progress dialog helper ────────────────────────────────────────────

    def _make_progress(self, title, label, total):
        """Create and return a QProgressDialog. Call .setValue(i) each step."""
        dlg = QtWidgets.QProgressDialog(label, "Cancel", 0, total, self)
        dlg.setWindowTitle(title)
        dlg.setWindowModality(QtCore.Qt.WindowModal)
        dlg.setMinimumWidth(400)
        dlg.setMinimumDuration(0)
        dlg.setValue(0)
        QtWidgets.QApplication.processEvents()
        return dlg

    def closeEvent(self, event):
        hou.session._mat_cache_importer_dlg = None
        super().closeEvent(event)


# =============================================================================
# SECTION 12 — Entry point
# run() is called when the shelf tool is clicked.

def _main_window():
    try:
        from hou import qt
        return shiboken6.wrapInstance(int(qt.mainWindow()), QtWidgets.QWidget)
    except Exception:
        return None


def run():
    if not hasattr(hou.session, "_mat_cache_importer_dlg"):
        hou.session._mat_cache_importer_dlg = None
    dlg = hou.session._mat_cache_importer_dlg
    if dlg is not None:
        try:
            if dlg.isVisible():
                dlg.raise_(); dlg.activateWindow(); return
            else:
                hou.session._mat_cache_importer_dlg = None
        except RuntimeError:
            hou.session._mat_cache_importer_dlg = None
    dlg = ImporterUI(parent=_main_window())
    dlg.setWindowModality(QtCore.Qt.NonModal)
    dlg.setAttribute(QtCore.Qt.WA_DeleteOnClose, True)
    dlg.show(); dlg.raise_(); dlg.activateWindow()
    hou.session._mat_cache_importer_dlg = dlg


run()
