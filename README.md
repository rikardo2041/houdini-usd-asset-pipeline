# houdini-usd-asset-pipeline
A Houdini 21 shelf tool that automates the full pipeline from raw 3D asset packs to a browsable USD component library — covering material import, geometry setup, USD export, component building, thumbnail generation, and Asset Catalog registration.

Houdini 21
Material & Cache Importer
User Guide & Pipeline Documentation
Made by Ricardo León
cutthefilm.rl@gmail.com


Overview
This tool automates the import, assignment, and USD export of third-party 3D asset packs into Houdini 21 using Karma MaterialX workflows. It handles everything from splitting a single combined FBX file to building complete USD component libraries with thumbnails.

The tool is a single Python script installed as a Houdini shelf tool. It presents a multi-tab dialog that guides you through the full pipeline in order.

Tab Overview
Tab
Purpose
⓪ Split Geometry
Optional. Split a single FBX/OBJ containing all assets into individual files per geometry.
① Textures → /mat
Scan a texture folder and build Karma MaterialX subnets in /mat.
② Caches → /obj
Import individual geometry files into one Geo node with Normal, Match Size, Name, and Material SOPs.
③ USD Export
Export each geometry as a .usd file with materials assigned in LOPs.
④ Component Builder
Build componentgeometry → materiallibrary → componentmaterial → componentoutput chains in /stage.
⑤ Component Export
Export component USDs to a library folder with viewport thumbnails.
⑥ Asset Catalog
Register component USDs into a Houdini Asset Catalog SQLite .db file with embedded thumbnails.


Pre-Flight: Asset Preparation
What to Check Before Starting
Before running the tool, verify the following about your source assets:

1. File Format
FBX or OBJ — the tool reads geometry files using Houdini's native File SOP.
BGEO, BGEO.SC, ABC (Alembic) are also supported for the Cache import tab.
FBX files are preferred as they preserve the name and shop_materialpath attributes reliably.
⚠ Note: If your pack comes as a single FBX with all assets combined, use Tab ⓪ to split it first.

2. Name Attribute
The tool relies heavily on the name prim attribute to identify geometry pieces and assign materials. Check your source files with:
file_sop.geometry().findPrimAttrib('name')
Name values can follow any of these conventions — the tool handles all of them:
Path format: ParentGroup/AssetName_wood — path format used by some packs
m-prefix format: mPackName_Concrete — m-prefix format used by some packs
Simple format: concrete_wall — lowercase simple format

3. shop_materialpath Attribute
Many asset packs include a shop_materialpath prim attribute. This is the most reliable way to assign materials because it directly names the material. Verify its presence:
geo.findPrimAttrib('shop_materialpath')
💡 Tip: If shop_materialpath exists, use the 'From @shop_materialpath' mode in Tab ② for the most accurate material assignment.

4. Texture Naming Conventions
The texture importer auto-detects map types from filename suffixes. Supported suffixes per map type:
Map Type
Recognised Suffixes
Base Color
basecolor, albedo, diffuse, base_color, color, col, bc, diff, alb
Roughness
roughness, rough, rgh, glossiness, gloss, gls
Metallic
metallic, metal, metalness, mtl, met
Normal
normal, nrm, nor, nml, norm
Height
height, hgt, disp, displacement
AO
ao, ambient_occlusion, ambientocclusion, occlusion
Opacity
opacity, alpha, transparency, transp
Emission
emission, emissive, emit, glow, emissivecolor
Specular
specular, spec


Tab ⓪ — Split Geometry (Optional)
Use this tab when your asset pack ships as a single FBX or OBJ file containing all geometries combined. It splits them into individual files based on the name prim attribute.

When to Use
Your pack ships as a single combined FBX or OBJ file
Each geometry in that file has a unique name attribute value
You need individual files for tabs ①②③④⑤ to process correctly

Step-by-Step
Click Browse… next to Source File and select your combined FBX or OBJ.
Click Browse… next to Output Folder and choose where individual files will be saved.
Set the Scale Multiplier. Many packs export in centimetres — use 0.01 to convert to metres. Check your pack's documentation.
Click ↻ Preview Geometries to see how many individual assets were detected from the name attribute.
Click ▶ Split and Export to export one file per geometry.

How It Works
For each unique name value in the source geometry, the tool creates:
groupsfromname — converts name attrib values into primitive groups
blast — isolates the target group (negate=1, Delete Non Selected)
attribdelete — keeps only name and shop_materialpath prim attribs, deletes all point attribs
groupdelete — removes all primitive and point groups
xform — applies the scale multiplier
name SOP — creates one name entry per unique shop_materialpath value (so each material becomes a named USD subset)
rop_fbx — exports with pathattrib=name so the name attribute is preserved in the FBX

Considerations
⚠ Note: Exported files are named after the last segment of the name path. The exported filename is the geometry name.
💡 Tip: After splitting, point Tab ② at the output folder. Make sure to uncheck Add Name Node in Tab ② since the split already handles naming.

Tab ① — Textures → /mat
Scans a texture folder and builds one Karma MaterialX material per group in /mat. Each material is a subnet containing mtlxstandard_surface, mtlxdisplacement, and the necessary image nodes.

Step-by-Step
Click Folder: Browse… and select the folder containing your textures.
The left panel shows detected material groups. The right panel shows the maps found for the selected group.
Set Displacement Scale (default 0.01). This is applied to the mtlxdisplacement node for any material with a height or displacement map.
Check Build Materials in the bottom bar.
Click ▶ Build Everything.

Material Structure in /mat
Each material is a Karma Material Builder subnet containing:
subinput — named 'inputs'
mtlxstandard_surface — all PBR maps wired in
mtlxdisplacement — wired from height/displacement map, scale set from UI
kma_material_properties — named 'material_properties'
Material_Outputs_and_AOVs — suboutput, inputs 0=surface, 1=displacement, 2=properties

Texture Grouping
Files are grouped by stem — everything before the first recognised suffix. For example:
chair_wood_basecolor.png → group: chair_wood
chair_wood_roughness.png → same group: chair_wood
💡 Tip: If textures aren't grouping correctly, check that suffix tokens are separated by underscore or hyphen.

Tab ② — Caches → /obj
Imports individual cache/geometry files into a single Geo node in /obj. Creates a node chain per geometry and assigns materials from /mat.

Step-by-Step
Click Folder: Browse… and select the folder containing your geometry files.
Check the geometries you want to import in the list.
Select the material group assignment method (see below).
Optionally enable Force Match for fuzzy material name matching.
Check or uncheck Add Name Node depending on your workflow.
Set the Geo node name in the bottom bar (default: caches).
Click ▶ Build Everything.

Node Chain Per Geometry
For each file, the tool creates this SOP chain inside /obj/caches:
file  →  normal  →  matchsize  →  [name]  →  material
normal — Weighting Method: By Face Area
matchsize — Justify Y: Min (places geometry flush to ground plane)
name — stamps name prim attrib per material group (toggle with Add Name Node)
material — one slot per material group, assigns /mat/<stem>

Material Group Assignment Modes
Mode
Best For
Auto-detect
Default. Detects name attrib vs primitive groups automatically per geometry.
From @name attribute
Packs where the name attrib contains path values like Grp/Part.
From primitive groups
Generic packs with simple primitive group names.
From group prefix:
Packs where material groups share a common prefix (e.g. mAsset_Concrete). Type the prefix in the text box.
From @shop_materialpath
Packs where the shop_materialpath attrib exactly names the material.


Force Match
When enabled, if no exact /mat node is found for a group, the tool tries keyword matching — splitting the group name into words and checking if any word appears in a /mat node name.
⚠ Note: Force Match can produce incorrect assignments when common words match multiple materials. Review the Material SOP after building.

Add Name Node Toggle
The Name SOP stamps the name prim attribute per material group so that sopimport can create USD geometry subsets.
Check — when geometry files don't already have a name attrib (most scenarios).
Uncheck — when you pre-processed with Tab ⓪ Split, which already sets name from shop_materialpath.

Tab ③ — USD Export
Exports each geometry as a .usd file with materials assigned. Creates a LOP chain in /stage per geometry.

Step-by-Step
Click Browse… and select the USD output folder.
Click ↻ Refresh Geo List to populate the geometry list from /obj/caches.
Select which geometries to export (Ctrl+click or Select All).
Click ▶ Export USD(s). A confirmation dialog will show the count and USD structure.
Click Export to proceed.

USD Structure
Each exported USD file follows this prim hierarchy:
/ASSET/<stem>/<subset_name>   — geometry subsets (one per material group)/ASSET/<mat_stem>/               — material prims

LOP Chain Per Geometry
sopimport  →  materiallibrary  →  assignmaterial  →  usd_rop
sopimport — imports from material_<stem> SOP. Prim path: /ASSET/<stem>
materiallibrary — imports unique materials at /ASSET/<mat_stem>/. matpathprefix=ASSET/
assignmaterial — one slot per group. primpattern=/ASSET/<stem>/<subset>, matspecpath=/ASSET/<mat_stem>/
usd_rop — exports to <output_folder>/<stem>.usd. flattensoplayers=1

💡 Tip: LOP nodes are only created if they don't already exist in /stage. Re-running export on an existing network just re-renders without rebuilding.

Tab ④ — Component Builder
Builds a USD component chain in /stage for each geometry, referencing the previously exported USD files. Run Tab ③ first.

Step-by-Step
Click Browse… next to USD Folder and select the folder where Tab ③ exported USDs.
Click ↻ Refresh List to populate geometries.
Select geometries and click ▶ Build Component Nodes in /stage.

LOP Chain Per Geometry
componentgeometry  →  materiallibrary  →  componentmaterial  →  componentoutput
componentgeometry — Source: Referenced Files. sourceusdref = exported USD path.
materiallibrary — matpathprefix=ASSET/. One entry per unique material.
componentmaterial — Input 0=comp_geo, Input 1=matlib. primpattern=/ASSET/geo/<subset>, matspecpath=/ASSET/<mat_stem>/
componentoutput — Named after geometry stem. Output path: <comp_folder>/assets/<stem>/<stem>.usd

Tab ⑤ — Component Export
Exports component USDs to your library folder and generates viewport thumbnails.

Step-by-Step
Click Browse… and select the component output folder.
Click ↻ Refresh List to populate componentoutput nodes from /stage.
Select components and click ▶ Export Component(s).

Thumbnail Generation
For each component, the tool:
Sets componentgeometry as the display node so the viewport shows the correct geometry.
Calls vp.frameAll() to fit the camera to that specific geometry.
Switches to the componentoutput node.
Presses executeviewport to capture a 512×512 viewport thumbnail.

💡 Tip: Keep the Houdini viewport visible and unobstructed during export. The thumbnail captures whatever is in the active viewport at the time of export.
⚠ Note: Thumbnails are captured sequentially. Exporting many components at once may take a while — plan accordingly.

Output Structure
<comp_folder>/  assets/    my_asset_a/      my_asset_a.usd      thumbnail.png    my_asset_b/      my_asset_b.usd      thumbnail.png

Tab ⑥ — Asset Catalog
Batch-registers all component USDs into a Houdini Asset Catalog SQLite (.db) database. Each asset’s thumbnail.png is embedded as a BLOB directly in the database so the Asset Catalog pane shows previews without re-rendering.

Step-by-Step
Click Browse… next to Component folder and select your component output folder (the one containing the assets/ subfolder).
Click Browse… next to Catalog .db to select an existing database, or click New… to create a fresh one with the correct schema.
Click ↻ Refresh to scan assets/<stem>/<stem>.usd entries.
Select all or specific assets.
Click ▶ Register in Asset Catalog.
Open a new Houdini session, open the Asset Catalog pane, and set its Read/Write database path to your .db file.

How It Works
The tool writes directly to the SQLite database that Houdini’s Asset Catalog pane reads. For each asset it inserts one row into the items table:
name — geometry stem (e.g. my_asset_name)
item_type — 'asset'
item_file — relative path: ./assets/<stem>/<stem>.usd
thumbnail — thumbnail.png bytes stored as BLOB
uuid — unique UUID generated per asset

⚠ Note: Houdini caches the catalog in memory. Always point the Asset Catalog pane at the .db file in a fresh Houdini session after registering assets.
💡 Tip: Duplicate detection is built in — if an asset already exists in the database it will be skipped on subsequent runs.

Complete Pipeline Walkthrough
Scenario A: Pack with Individual Files per Asset
Best for: Packs where each asset ships as an individual FBX or OBJ file.
Tab ①  — Point at texture folder. Set displacement scale. Build.
Tab ②  — Point at geometry folder. Choose 'From group prefix: m'. Check Force Match if needed. Build.
Tab ③  — Set output folder. Refresh. Select all. Export.
Tab ④  — Point at USD folder. Refresh. Build components.
Tab ⑤  — Set library folder. Export all with thumbnails.
Tab ⑥  — Point at component folder + .db file. Register all assets.

Scenario B: Single Combined File with shop_materialpath
Best for: Packs that ship as one combined FBX or OBJ containing all assets.
Tab ⓪  — Split the combined FBX. Set scale (usually 0.01). Export to a split folder.
Tab ①  — Build materials from texture folder.
Tab ②  — Point at split folder. Choose 'From @shop_materialpath'. Uncheck Add Name Node. Check Force Match. Build.
Tab ③  — Export USDs.
Tab ④  — Build components.
Tab ⑤  — Export with thumbnails.
Tab ⑥  — Register all assets into catalog .db.

Scenario C: Packs with Path-format Name Attributes
Best for: Packs where the name prim attribute follows a path format (e.g. Group/Part).
Tab ①  — Build materials.
Tab ②  — Auto-detect mode. Check Force Match. Build.
Tab ③ → ④ → ⑤ → ⑥  — Standard export and catalog flow.

Troubleshooting
Materials not assigning in Material SOP
Check that /mat was populated first (Tab ① before Tab ②).
Try switching the assignment mode — if auto-detect picks wrong mode, override manually.
Enable Force Match for packs where material names don't exactly match geometry group names.
Unmatched slots are left empty — fill them manually in the Material SOP.

USD shows wrong geometry subset paths
If primpattern in assignmaterial doesn't match what sopimport creates, check the name attrib values on the geometry after building.
Run the debug snippet: geo.primStringAttribValues('name') to see actual values.

Thumbnails capturing wrong geometry
Keep the Houdini viewport open and unobstructed during component export.
The tool sets the display flag and calls frameAll() per component — if the viewport isn't refreshing, increase the sleep time in the script.

Split tab exporting 8kb files
The blast group name must match the groupsfromname output exactly (/ replaced with _).
Verify by running the groupsfromname debug snippet and comparing group names.

Name attribute overwritten after FBX export
The rop_fbx node uses pathattrib=name and buildfrompath=1 — verify these are set on the rop_fbx node.
After import, check the name attrib with the spreadsheet editor.

Assets not appearing in Asset Catalog pane after registering
Houdini caches the catalog in memory and does not hot-reload the .db file while running.
Always open a fresh Houdini session after registering, then point the Asset Catalog pane at the .db file.
The .db file must be in the parent folder of the assets/ directory for relative paths to resolve.
Verify the database has entries: run sqlite3 check in Python (see debug snippets below).

USD Prim Path Reference
Context
Geometry Prim
Material Prim
USD Export
/ASSET/<stem>/<subset>
/ASSET/<mat_stem>/
Component
/ASSET/geo/<subset>
/ASSET/<mat_stem>/


Quick Reference — Debug Snippets
Run these in the Houdini Python editor to diagnose issues:

Check name attrib values on a geometry
node = hou.node('/obj/caches/YOUR_FILE_NODE')geo  = node.geometry()vals = sorted(set(geo.primStringAttribValues('name')))for v in vals: print(v)

Check shop_materialpath values
node = hou.node('/obj/caches/YOUR_FILE_NODE')geo  = node.geometry()vals = sorted(set(geo.primStringAttribValues('shop_materialpath')))for v in vals: print(v)

List all materials in /mat
for n in hou.node('/mat').children(): print(n.name())

Check Material SOP assignments
mat = hou.node('/obj/caches/material_YOUR_STEM')n   = mat.parm('num_materials').eval()for i in range(1, n+1):    print(mat.parm(f'group{i}').eval(), '->', mat.parm(f'shop_materialpath{i}').eval())

Houdini 21 Material & Cache Importer — Pipeline Documentation
