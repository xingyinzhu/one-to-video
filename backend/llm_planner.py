"""AI 導演分鏡規劃器（支援：免費 Gemini Key / 本機 Ollama / 免 Key 貼上解析與智慧初稿）。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Tuple

import httpx

DEFAULT_NEGATIVE_PROMPT = (
    "cartoon, anime, 3d render, cgi, video game, low poly, text, watermark, logo, "
    "subtitles, blurry, shaky camera, distorted architecture, oversaturated, neon colors, "
    "deformed hands, extra limbs, glitch"
)

VISUAL_STYLE_CONFIG: Dict[str, Dict[str, str]] = {
    "35mm_doc": {
        "label": "35mm 寫實旅遊紀錄片",
        "style_lock": "自然黃金時刻光影 ＋ 35mm 電影紀錄片鏡頭質感 ＋ 真實建築與自然紋理 ＋ 穩定流暢運鏡（無字幕無浮水印）",
        "en_suffix": "shot on 35mm cinema camera, ultra-realistic 4K travel documentary, natural golden lighting, subtle film grain, steady camera movement, no text, no watermark.",
    },
    "aerial_epic": {
        "label": "電影航拍大片",
        "style_lock": "高空史詩航拍視角 ＋ IMAX 級壯闊景深 ＋ 戲劇性光影與雲層層次 ＋ 極致平穩推進運鏡（無字幕無浮水印）",
        "en_suffix": "epic cinematic aerial drone footage, shot on IMAX cinema camera, sweeping panoramic vista, dramatic volumetric lighting, ultra-realistic 4K documentary, smooth motion, no text, no watermark.",
    },
    "film_vlog": {
        "label": "唯美膠片 Vlog",
        "style_lock": "Kodak Portra 400 溫潤底片色調 ＋ 第一人稱沉浸漫步視角 ＋ 柔和淺景深與溫暖逆光（無字幕無浮水印）",
        "en_suffix": "cinematic Kodak Portra 400 film aesthetic, warm nostalgic tones, gentle shallow depth of field, smooth handheld gimbal movement, authentic travel film look, 4K realistic video, no text, no watermark.",
    },
}


def recommended_shot_count(target_duration: int) -> int:
    """根據目標時長換算建議分鏡/幕數（45 秒採 MiniMax H3 黃金 4 幕：15s + 10s×3）。"""
    if target_duration <= 48:
        return 4
    if target_duration <= 70:
        return 8
    return 12


def format_prompt_prefix(aspect_ratio: str, visual_style: str) -> str:
    orientation = (
        "16:9 landscape" if aspect_ratio == "16:9" else "9:16 vertical portrait"
    )
    style_desc = {
        "35mm_doc": "realistic documentary",
        "aerial_epic": "cinematic aerial documentary",
        "film_vlog": "warm film-look travel",
    }.get(visual_style, "realistic documentary")
    return f"Generate a {orientation} {style_desc} video: "


def ensure_full_english_prompt(raw_prompt: str, aspect_ratio: str, visual_style: str) -> str:
    """確保英文 Prompt 具備明確比例前綴與風格結尾規範。"""
    p = (raw_prompt or "").strip().strip("`").strip()
    prefix = format_prompt_prefix(aspect_ratio, visual_style)
    suffix = VISUAL_STYLE_CONFIG.get(visual_style, VISUAL_STYLE_CONFIG["35mm_doc"])["en_suffix"]

    lower_p = p.lower()
    if not (
        lower_p.startswith("generate a 16:9")
        or lower_p.startswith("generate a 9:16")
    ):
        p = prefix + p

    if "no text" not in p.lower():
        p = p.rstrip(".") + f", {suffix}"
    return p


def build_master_prompt_for_web(
    brief: str,
    target_duration: int = 60,
    aspect_ratio: str = "16:9",
    visual_style: str = "35mm_doc",
) -> str:
    """產生可一鍵複製到 Gemini 網頁版 / ChatGPT 的總指令。"""
    shot_cnt = recommended_shot_count(target_duration)
    per_shot_sec = round((target_duration + (shot_cnt - 1) * 0.5) / shot_cnt, 1)
    style_cfg = VISUAL_STYLE_CONFIG.get(visual_style, VISUAL_STYLE_CONFIG["35mm_doc"])
    prefix = format_prompt_prefix(aspect_ratio, visual_style)

    return f"""你是一位國際級紀錄片導演與 AI 視覺分鏡師。請根據以下一句話需求，規劃一份可直接交給 Google Flow (Veo) 生成影片的完整 {shot_cnt} 鏡分鏡表。

【影片需求】：{brief}
【目標總時長】：約 {target_duration} 秒（共 {shot_cnt} 鏡，每鏡約 {per_shot_sec} 秒）
【畫面比例】：{aspect_ratio}
【視覺風格】：{style_cfg["label"]}

請嚴格只輸出一段純 JSON（不要加其他解釋文字），格式如下：
```json
{{
  "style_lock": "請用繁體中文寫出全片統一的光影、色調與鏡頭語言規範",
  "negative_prompt": "{DEFAULT_NEGATIVE_PROMPT}",
  "shots": [
    {{
      "shot_order": 1,
      "title": "簡短繁體中文鏡頭標題（含景別/運鏡）",
      "duration_sec": {per_shot_sec},
      "scene_desc_zh": "繁體中文詳細畫面與運鏡描述",
      "voiceover_zh": "適合後期配音的一句繁體中文旁白靈感",
      "prompt_en": "{prefix}<Detailed cinematic English visual description with camera movement, lighting, textures, 35mm documentary look, no text, no watermark.>"
    }}
  ]
}}
```
請務必產出完整 {shot_cnt} 個鏡頭（shot_order 1 到 {shot_cnt}），按合理敘事或空間遊覽動線排列，且每段 prompt_en 必須以 `{prefix}` 開頭！"""


# -------------------------
# 內建主題與通用智慧分鏡產生器（免 Key 0 延遲初稿）
# -------------------------
_WINDSOR_SHOTS = [
    {
        "title": "長步道空拍開場（The Long Walk）",
        "scene_desc_zh": "高空航拍沿著筆直寬闊的「長步道」緩慢向前推進，兩側古樹成蔭，晨霧微散，遠方溫莎城堡高踞綠丘之上，圓塔在金色陽光下格外宏偉。",
        "voiceover_zh": "在倫敦以西的泰晤士河畔，矗立著世界上最古老、仍在使用的王室城堡——溫莎城堡。",
        "prompt_en": "Cinematic aerial drone shot slowly flying forward along The Long Walk in Windsor Great Park toward Windsor Castle in the distance, flanked by lush green oak trees and morning mist, the historic limestone castle and iconic Round Tower rising majestically on the hill, warm golden morning sunlight",
    },
    {
        "title": "步入亨利八世門（Henry VIII's Gate）",
        "scene_desc_zh": "模擬遊客視角的平穩推進鏡頭，沿著古老石板路走近並穿過宏偉的亨利八世門，中世紀石造拱門與斜射陽光營造走進歷史的沉浸感。",
        "voiceover_zh": "穿過這座五百年前由亨利八世修建的石拱門，九百年的英國王室歷史在眼前展開。",
        "prompt_en": "Smooth eye-level gimbal tracking shot walking slowly along a historic cobblestone street toward and through Henry VIII's Gate at Windsor Castle, weathered medieval limestone walls and Gothic stone archway illuminated by warm angled sunlight, rich architectural stone textures",
    },
    {
        "title": "仰望圓塔與護城河花園（The Round Tower）",
        "scene_desc_zh": "鏡頭從盛開的護城河花園緩慢向上仰拍並微微環繞，展現溫莎城堡核心地標圓塔，塔頂旗幟在藍天白雲下隨風飄動。",
        "voiceover_zh": "這座高聳的圓塔是城堡的心臟，由征服者威廉奠基，至今仍是英國君主駐蹕的象徵。",
        "prompt_en": "Slow cinematic upward tilt and gentle orbital shot of the iconic Round Tower of Windsor Castle rising above the manicured green lawn and blooming flowers of the Moat Garden, historic grey medieval stone battlement walls, a royal flag gently waving against a soft blue sky",
    },
    {
        "title": "下區中庭與衛兵交接（Changing of the Guard）",
        "scene_desc_zh": "紀錄片風格中景緩慢橫移，身穿鮮紅禮服與黑色高筒熊皮帽的皇家衛兵整齊邁步走過溫莎城堡石板中庭。",
        "voiceover_zh": "來到溫莎，絕不能錯過莊嚴而經典的衛兵交接儀式，見證延續數百年的王室傳統。",
        "prompt_en": "Cinematic medium dolly shot in the cobblestone courtyard of Windsor Castle as British King's Guards in traditional scarlet tunics and tall black bearskin hats march in crisp formation across the historic Lower Ward, stately medieval stone facades in warm natural sunlight, shallow depth of field",
    },
    {
        "title": "聖喬治禮拜堂哥德外觀（St George's Chapel）",
        "scene_desc_zh": "柔和橫移鏡頭展示聖喬治禮拜堂華麗的英國垂直哥德式建築外觀、高聳飛扶壁與精細石雕尖塔。",
        "voiceover_zh": "一旁的聖喬治禮拜堂，是英國哥德式建築的巔峰之作，也是多場王室歷史儀式的見證地。",
        "prompt_en": "Slow smooth sideways tracking shot showcasing the magnificent Gothic exterior of St George's Chapel at Windsor Castle, intricate Perpendicular Gothic limestone carvings, soaring flying buttresses, pointed pinnacles, and ornate arched windows catching warm golden afternoon light",
    },
    {
        "title": "禮拜堂扇形拱頂與彩繪玻璃光影",
        "scene_desc_zh": "鏡頭緩慢步入莊嚴靜謐的哥德式大廳，仰望繁複如蕾絲般的扇形石拱頂，午後陽光穿過彩色玻璃窗灑下金色丁達爾光束。",
        "voiceover_zh": "走進室內，陽光穿過百年彩繪玻璃，映照著精雕細琢的扇形拱頂，彷彿時間在這裡靜止。",
        "prompt_en": "Slow serene forward dolly shot inside a grand historic English Gothic chapel inspired by St George's Chapel at Windsor, soaring intricate stone fan-vaulted ceilings, dark carved oak choir stalls, warm golden sunbeams streaming through tall stained glass windows illuminating subtle dust motes",
    },
    {
        "title": "北露台漫步與泰晤士河谷遠眺",
        "scene_desc_zh": "傍晚柔和暖光下，鏡頭沿著城堡高處的北露台石砌女牆平穩滑過，俯瞰綠意盎然的泰晤士河谷與遠方英倫田園風光。",
        "voiceover_zh": "站在城堡北露台遠眺，泰晤士河谷的英倫田園風光盡收眼底。",
        "prompt_en": "Smooth cinematic gliding shot along the historic stone North Terrace walkway of Windsor Castle during late afternoon golden hour, ancient castle stone parapet on the left opening up to sweeping panoramic views of the lush green Thames Valley countryside",
    },
    {
        "title": "夕陽下的溫莎城堡遠景收尾",
        "scene_desc_zh": "史詩感高空航拍緩慢拉遠並微升，整座溫莎城堡在橘金色日落餘暉中閃耀，城牆剪影映襯著蜿蜒閃亮的泰晤士河。",
        "voiceover_zh": "當夕陽為古老的城牆鍍上金邊，這場穿越九百年的溫莎之旅，也在暮色中落下帷幕。",
        "prompt_en": "Epic cinematic aerial drone shot slowly pulling backward and ascending from Windsor Castle during sunset golden hour, the entire historic limestone fortress, Round Tower, and battlements glowing in warm amber twilight, the River Thames winding gently nearby reflecting the golden sky",
    },
]

_KYOTO_SHOTS = [
    {
        "title": "晨霧中的京都古寺山門航拍",
        "scene_desc_zh": "清晨薄霧籠罩東山群峰，高空鏡頭緩慢推向掩映在火紅楓林中的古老木造寺院山門，朝陽灑下柔和金光。",
        "voiceover_zh": "深秋清晨的京都，當第一縷陽光穿透山間薄霧，千年古寺在楓紅中靜靜甦醒。",
        "prompt_en": "Cinematic aerial drone shot gliding slowly over misty autumn hills in Kyoto toward an ancient wooden Buddhist temple complex nestled among vibrant crimson and golden maple trees, soft golden morning sunlight breaking through light fog",
    },
    {
        "title": "石坂小徑與紅楓落葉推進",
        "scene_desc_zh": "低角度穩定器推進鏡頭，沿著鋪滿紅楓落葉的濕潤石板參道緩步向前，兩側苔蘚石燈籠古意盎然。",
        "voiceover_zh": "踏上鋪滿紅葉的石板參道，每一步都像走進一幅流動的秋日畫卷。",
        "prompt_en": "Smooth low-angle gimbal tracking shot moving forward along a mossy stone pathway carpeted with fallen red maple leaves leading to a historic Kyoto temple, weathered stone lanterns on both sides, gentle morning light",
    },
    {
        "title": "仰望木造本堂飛簷與楓葉交錯",
        "scene_desc_zh": "鏡頭透過前景隨風輕搖的火紅楓枝，緩慢仰拍古寺厚重的檜木斗拱與優雅上揚的飛簷。",
        "voiceover_zh": "百年的檜木飛簷與似火紅楓相互映襯，展現出東方建築獨有的內斂與詩意。",
        "prompt_en": "Slow cinematic upward tilt framing traditional dark cedar temple eaves and intricate wooden brackets through foreground branches of glowing red Japanese maple leaves swaying gently in the autumn breeze, backlit warm sunlight",
    },
    {
        "title": "手水舍竹筒流水特寫",
        "scene_desc_zh": "淺景深特寫鏡頭，清澈山泉從青竹筒緩緩注入長滿翠綠青苔的石鉢，水面泛起細緻漣漪並倒映紅楓。",
        "voiceover_zh": "潺潺竹水洗去塵囂，水面上倒映著一抹屬於秋天的緋紅。",
        "prompt_en": "Close-up shallow depth of field documentary shot of clear mountain water trickling from a weathered bamboo spout into a moss-covered stone basin at a Kyoto shrine, subtle ripples reflecting red autumn leaves above",
    },
    {
        "title": "枯山水庭園光影橫移",
        "scene_desc_zh": "平穩橫移鏡頭掠過靜謐的枯山水白砂庭園，整齊砂紋與蒼勁青苔奇石在斜陽下拉出修長影子。",
        "voiceover_zh": "在一方枯山水之間，白砂化作海洋、頑石凝為群山，萬物歸於沉靜。",
        "prompt_en": "Slow serene lateral dolly shot across a traditional Zen rock garden in Kyoto, meticulously raked white gravel waves surrounding mossy stones, long afternoon shadows cast across the courtyard",
    },
    {
        "title": "木質緣側長廊與障子門暖光",
        "scene_desc_zh": "沿著寺院溫潤反光的木造緣側走廊緩慢推進，一側是紙障子門柔和透光，另一側敞開迎向滿園秋色。",
        "voiceover_zh": "順著歲月打磨的木廊漫步，光與影在紙門與庭園之間溫柔對話。",
        "prompt_en": "Smooth forward tracking shot along a polished historic wooden engawa veranda of a Kyoto temple, warm golden afternoon light reflecting off aged wood floors, shoji screens on one side and vibrant autumn maple garden on the other",
    },
    {
        "title": "清水舞台遠眺京都暮色全景",
        "scene_desc_zh": "黃昏黃金時刻從高處木造露台俯瞰整片紅葉谷地與遠方古都屋瓦天際線，夕陽染紅天際。",
        "voiceover_zh": "憑欄遠眺，漫山遍野的秋楓與古都的萬家屋瓦，在夕陽下融為一體。",
        "prompt_en": "Sweeping cinematic wide shot from an elevated wooden temple terrace overlooking a sea of red maple trees and traditional Kyoto tiled rooftops in the valley below during golden hour sunset",
    },
    {
        "title": "暮色石燈籠點亮收尾",
        "scene_desc_zh": "藍調暮色初降，古老石燈籠透出溫暖燭光，鏡頭緩慢後拉，微風吹落幾片紅楓，靜謐收尾。",
        "voiceover_zh": "暮色四合，古寺燈火微明，將這場京都秋日的溫柔記憶永遠定格。",
        "prompt_en": "Peaceful cinematic pull-back shot at blue hour dusk in a quiet Kyoto temple courtyard, warm amber light glowing softly inside an ancient stone lantern while a few red maple leaves drift gently in the air",
    },
]


_REGENT_STREET_CHRISTMAS_SHOTS = [
    {
        "title": "暮色降臨：攝政街金色天使燈海全景開場",
        "scene_desc_zh": "聖誕前夕的倫敦藍調暮色中，高空搖臂鏡頭沿著攝政街優雅的弧形街道緩緩向前推進；半空中巨型「聖誕精靈」金色天使展開璀璨光翼橫跨波特蘭石歷史建築，紅色雙層巴士與黑色計程車在微濕路面流光倒影中平穩穿梭。",
        "voiceover_zh": "當十二月的暮色降臨倫敦，整條攝政街點亮金色光翼，宛如一條流淌在百年建築間的星河。",
        "prompt_en": "Cinematic sweeping elevated crane shot slowly gliding forward along the iconic curved avenue of Regent Street in London at blue hour dusk before Christmas, gigantic illuminated golden Spirit of Christmas angels with radiant outstretched wings suspended high across grand neoclassical white Portland stone facades, classic red London double-decker buses and glossy black cabs driving smoothly over rain-slicked asphalt reflecting warm amber and ruby lights, festive twilight atmosphere",
    },
    {
        "title": "街頭沉浸：雙層紅巴士與暖光聖誕櫥窗漫步",
        "scene_desc_zh": "無縫承接鏡頭高度緩降到街頭平視視角，穩定器沿著熱鬧溫暖的人行道平穩推進；兩側百年精品店櫥窗點綴常青松枝花環與暖金燈串，身穿冬季大衣的行人在頭頂璀璨天使光幕下漫步，一輛紅色雙層巴士在淺景深暖光散景中緩緩駛過。",
        "voiceover_zh": "走進微涼的冬夜街頭，紅色雙層巴士緩緩駛過，兩旁溫暖的節日櫥窗映照著人們期待聖誕的笑臉。",
        "prompt_en": "Seamless continuous camera descent to smooth eye-level gimbal tracking shot walking slowly down the bustling sidewalk of Regent Street London before Christmas, warm glowing luxury shopfront windows decorated with evergreen pine wreaths, golden fairy lights and red ornaments, winter pedestrians in wool coats strolling beneath the luminous golden angel canopy overhead, a classic red London double-decker bus gliding slowly past in creamy golden bokeh, authentic London winter evening",
    },
    {
        "title": "光影特寫：金色天使光翼與維多利亞街燈交錯",
        "scene_desc_zh": "鏡頭平滑微微上仰並柔和橫移，細膩展現高掛空中的金色聖誕天使燈串在深藍夜空下閃耀的晶瑩光點，景深自然過渡至弧形石造陽台、發光的復古街燈與街角紅金配色的聖誕裝飾細節。",
        "voiceover_zh": "抬頭仰望，數十萬顆暖金燈珠勾勒出守護倫敦冬夜的金色翅膀，讓整座城市沉浸在溫柔的節慶光芒裡。",
        "prompt_en": "Seamless smooth upward tilt and gentle lateral dolly shot gliding past the intricate shimmering golden light strands of the overhead Spirit of Christmas angel installations glowing against the deep indigo London twilight sky, shallow depth of field shifting delicately across historic curved Georgian stone balconies, glowing vintage streetlamps, and festive red-and-gold holiday wreaths reflecting softly on damp pavement",
    },
    {
        "title": "璀璨收尾：俯瞰攝政街金色銀河緩緩拉遠",
        "scene_desc_zh": "史詩感航拍鏡頭緩慢上升並向後拉遠，從高處完整俯瞰整條攝政街綿延向皮卡迪利圓環的金色天使燈海長廊，川流不息的車燈化作溫暖光帶，為倫敦聖誕前夕的浪漫之夜畫下優雅句點。",
        "voiceover_zh": "從高處回望這片綿延不絕的金色燈海，聖誕前夕的攝政街，正上演著倫敦一年之中最動人的冬日序曲。",
        "prompt_en": "Seamless epic cinematic aerial pull-back shot slowly ascending above Regent Street looking down the entire glowing golden ribbon of illuminated Christmas angels stretching gracefully toward Piccadilly Circus in London, flowing streams of warm golden headlights and ruby taillights weaving beneath the luminous festive canopy, grand historic illuminated facades on both sides, magical heartwarming London Christmas eve documentary finale",
    },
]


_WESTMINSTER_ABBEY_SHOTS = [
    {
        "title": "晨光高空航拍威斯敏斯特教堂與國會廣場",
        "scene_desc_zh": "清晨柔和金光下，高空航拍緩慢向前推進，俯瞰白色石灰岩哥德式雙塔的威斯敏斯特教堂與周邊國會廣場、泰晤士河畔的倫敦天際線。",
        "voiceover_zh": "在倫敦的心臟地帶，威斯敏斯特教堂靜靜矗立了近千年，見證了英國王室的加冕與歷史。",
        "prompt_en": "Wide establishing aerial drone shot slowly flying forward over Westminster Abbey in London, the grand Gothic twin western towers and white limestone facade glowing in soft golden morning sunlight, Parliament Square and the River Thames in the distance, light morning mist over the city skyline",
    },
    {
        "title": "街頭平視步向西大門與哥德雙塔",
        "scene_desc_zh": "穩定器平視鏡頭沿著人行道緩步走向威斯敏斯特教堂西大門，高聳的雙塔與精細石雕立面在斜射陽光下逐步佔滿畫面。",
        "voiceover_zh": "走近教堂西大門，歷代君王走過的石板路就在腳下，哥德式尖塔直指天空。",
        "prompt_en": "Smooth eye-level gimbal tracking shot walking slowly toward the Great West Door of Westminster Abbey in London, visitors strolling on the pavement, the soaring Gothic twin towers and intricately carved pale limestone facade rising ahead, warm angled afternoon sunlight casting gentle shadows",
    },
    {
        "title": "仰拍北立面飛扶壁與石雕尖塔",
        "scene_desc_zh": "低角度緩慢上仰並微幅橫移，展現北入口華麗的哥德式拱門、飛扶壁與精緻石雕尖塔，背景是柔和的藍天白雲。",
        "voiceover_zh": "飛扶壁與尖塔交織出向上的力量，每一道石雕都是中世紀工匠留下的細節。",
        "prompt_en": "Low-angle slow upward tilt with gentle lateral movement along the North Entrance of Westminster Abbey, ornate Gothic portal, graceful flying buttresses and pointed stone pinnacles reaching toward a soft blue sky with light clouds, crisp weathered limestone details",
    },
    {
        "title": "步入哥德式主殿中廊與金色光束",
        "scene_desc_zh": "鏡頭在莊嚴的哥德式主殿中廊緩慢前推，兩側高聳的肋拱石柱向上延伸，彩色玻璃窗投下金色光束。",
        "voiceover_zh": "步入主殿，數十公尺高的拱頂讓人不自覺放輕腳步，光線在石柱間緩緩流動。",
        "prompt_en": "Interior slow forward dolly shot down the central nave of Westminster Abbey, towering ribbed Gothic stone vaults and clustered columns soaring overhead, golden sunbeams streaming through tall stained glass windows illuminating floating dust motes, hushed majestic atmosphere",
    },
    {
        "title": "彩繪玫瑰花窗與石雕細節特寫",
        "scene_desc_zh": "淺景深特寫，聚焦發光的彩繪玫瑰花窗與歷經數百年風化的石雕紋理，光影在彩色玻璃與石面間流動。",
        "voiceover_zh": "彩繪玻璃把陽光化成顏色，而古老石雕上的每一道刻痕，都是時間的簽名。",
        "prompt_en": "Intimate close-up shot with shallow depth of field of the glowing stained-glass rose window inside Westminster Abbey, jewel-toned light spilling onto weathered medieval stone carvings and tracery, subtle slow camera drift",
    },
    {
        "title": "靜謐迴廊拱門與中庭花園橫移",
        "scene_desc_zh": "平穩橫移鏡頭沿著古老石砌迴廊拱門滑行，拱門框出翠綠的中庭花園，斜陽在石地板上拉出長長的光影。",
        "voiceover_zh": "走進迴廊，喧囂瞬間遠去，只剩下石拱門與中庭花園裡的一方寧靜。",
        "prompt_en": "Smooth lateral dolly shot gliding along the historic Gothic stone cloisters of Westminster Abbey, rhythmic pointed arches framing a lush green courtyard garden, long afternoon shadows across worn flagstone floors, serene contemplative mood",
    },
    {
        "title": "亨利七世禮拜堂扇形拱頂仰拍",
        "scene_desc_zh": "鏡頭緩慢仰拍亨利七世禮拜堂繁複如蕾絲般的扇形石拱頂，兩側深色橡木座椅與垂掛的騎士旗幟營造莊嚴氛圍。",
        "voiceover_zh": "抬頭仰望，扇形拱頂像石頭織成的蕾絲，是英國哥德晚期建築最動人的篇章。",
        "prompt_en": "Slow cinematic upward tilt revealing the intricate stone fan-vaulted ceiling of the Henry VII Lady Chapel in Westminster Abbey, dark carved oak stalls and heraldic knightly banners below, warm soft light filtering from high windows",
    },
    {
        "title": "夕陽黃金時刻拉遠俯瞰教堂與泰晤士河畔",
        "scene_desc_zh": "日落黃金時刻，航拍鏡頭緩慢拉遠並微升，威斯敏斯特教堂的石灰岩立面染上橘金暖光，遠處伊莉莎白塔（大笨鐘）與泰晤士河閃耀倒影。",
        "voiceover_zh": "夕陽為古老的石牆鍍上金邊，這座見證歷史的教堂，在暮色中安靜地結束這一天。",
        "prompt_en": "Epic cinematic aerial pull-back shot slowly ascending away from Westminster Abbey at sunset golden hour, pale limestone towers glowing in warm amber light, the Elizabeth Tower and the River Thames reflecting a rose-gold sky in the distance, peaceful majestic documentary ending",
    },
]


_CN_TOPIC_TRANSLATIONS = [
    ("威斯敏斯特教堂", "Westminster Abbey in London"),
    ("西敏寺", "Westminster Abbey in London"),
    ("大笨鐘", "Big Ben and the Elizabeth Tower in London"),
    ("白金漢宮", "Buckingham Palace in London"),
    ("大英博物館", "the British Museum in London"),
    ("倫敦塔橋", "Tower Bridge in London"),
    ("倫敦", "London"),
    ("巴黎鐵塔", "the Eiffel Tower in Paris"),
    ("艾菲爾鐵塔", "the Eiffel Tower in Paris"),
    ("羅浮宮", "the Louvre Museum in Paris"),
    ("巴黎聖母院", "Notre-Dame de Paris"),
    ("巴黎", "Paris"),
    ("羅馬競技場", "the Colosseum in Rome"),
    ("羅馬", "Rome"),
    ("威尼斯", "Venice with its canals"),
    ("富士山", "Mount Fuji in Japan"),
    ("東京", "Tokyo"),
    ("京都", "Kyoto"),
    ("阿爾卑斯山", "the Swiss Alps"),
    ("聖家堂", "the Sagrada Familia in Barcelona"),
    ("紐約", "New York City"),
    ("自由女神", "the Statue of Liberty"),
    ("長城", "the Great Wall of China"),
    ("故宮", "the Forbidden City in Beijing"),
    ("古堡", "an ancient medieval castle"),
    ("教堂", "a historic Gothic cathedral"),
    ("老街", "a historic old town street"),
]


def translate_cn_topic_to_english(topic: str) -> str:
    """將常見中文地標／景點轉為具體英文名詞；無法對應時回傳原文字。"""
    for cn, en in _CN_TOPIC_TRANSLATIONS:
        if cn in topic:
            return en
    return topic


def generate_smart_draft(
    brief: str,
    target_duration: int = 60,
    aspect_ratio: str = "16:9",
    visual_style: str = "35mm_doc",
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    免 API Key 智慧導演初稿產生器：
    根據使用者的一句話主題、時長、比例與風格，立即產出結構完整的分鏡表初稿。
    """
    shot_cnt = recommended_shot_count(target_duration)
    per_shot_sec = round((target_duration + (shot_cnt - 1) * 0.5) / shot_cnt, 1)
    style_cfg = VISUAL_STYLE_CONFIG.get(visual_style, VISUAL_STYLE_CONFIG["35mm_doc"])
    style_lock = style_cfg["style_lock"]

    lower_brief = brief.lower()
    if "攝政" in brief or "摄政" in brief or "regent street" in lower_brief or ("倫敦" in brief and "聖誕" in brief) or ("伦敦" in brief and "圣诞" in brief):
        base_pool = _REGENT_STREET_CHRISTMAS_SHOTS
        style_lock = "倫敦聖誕前夕藍調暮色與暖金天使燈海光影 ＋ 35mm 電影實拍紀錄片質感 ＋ 濕潤路面流光倒影與紅巴士色彩點綴 ＋ 極致平穩連貫運鏡"
    elif "威斯敏斯特" in brief or "西敏寺" in brief or "westminster" in lower_brief:
        base_pool = _WESTMINSTER_ABBEY_SHOTS
        style_lock = "倫敦晴朗黃金時刻自然光 ＋ 35mm 電影紀錄片質感 ＋ 白色石灰岩與哥德式石雕細節 ＋ 多機位切換（空拍／街景／仰拍／室內／特寫）＋ 穩定流暢運鏡"
    elif "溫莎" in brief or "windsor" in lower_brief:
        base_pool = _WINDSOR_SHOTS
        style_lock = "英國晴朗黃金時刻自然光 ＋ 35mm 電影紀錄片質感 ＋ 溫潤中世紀石灰岩色調 ＋ 穩定流暢運鏡"
    elif "京都" in brief or "kyoto" in lower_brief or "古寺" in brief:
        base_pool = _KYOTO_SHOTS
        style_lock = "日本深秋柔和晨昏自然光 ＋ 35mm 紀實膠片質感 ＋ 古木與紅楓溫潤色調 ＋ 靜謐平穩運鏡"
    else:
        # 針對任意自訂主題自動萃取關鍵主體並展開經典紀錄片敘事弧線
        topic = (
            brief.replace("我要做一條", "")
            .replace("我要做一支", "")
            .replace("無聲方便後期配音", "")
            .replace("純畫面無聲", "")
            .replace("1 分鐘", "")
            .replace("1分鐘", "")
            .replace("30 秒", "")
            .replace("90 秒", "")
            .replace("橫屏", "")
            .replace("豎屏", "")
            .strip(" ，。、！？,.!?")
        ) or brief.strip()
        topic_en = translate_cn_topic_to_english(topic)

        base_pool = [
            {
                "title": f"晨光高空遠景開場｜{topic[:12]}",
                "scene_desc_zh": f"清晨柔和金色陽光下，高空航拍緩慢向前推進，宏觀展現「{topic}」的整體環境地貌與壯闊開場氛圍。",
                "voiceover_zh": f"當晨光灑落大地，我們即將走進「{topic}」，展開一段沉浸式的視覺探索。",
                "prompt_en": f"Wide establishing aerial drone shot slowly flying forward revealing the breathtaking landscape and iconic setting of {topic_en}, soft golden morning sunlight, atmospheric morning mist",
            },
            {
                "title": "第一人稱沉浸步入動線",
                "scene_desc_zh": f"平穩推進的平視鏡頭，沿著入口步道緩慢走入「{topic}」核心場域，前景光影交錯帶來身歷其境感。",
                "voiceover_zh": "順著蜿蜒的動線緩步前行，周圍的空氣與光影開始訴說屬於這裡的故事。",
                "prompt_en": f"Smooth eye-level gimbal tracking shot walking slowly forward along the entrance pathway into {topic_en}, rich environmental textures, warm angled natural light casting gentle shadows",
            },
            {
                "title": "核心主體仰拍環繞揭示",
                "scene_desc_zh": f"鏡頭從低角度緩慢仰拍並微幅環繞，突顯「{topic}」最具代表性的主體結構與天際線輪廓。",
                "voiceover_zh": "抬頭仰望，最具標誌性的景象矗立眼前，展現出令人屏息的空間張力。",
                "prompt_en": f"Slow cinematic upward tilt and subtle orbital camera movement showcasing the main iconic centerpiece of {topic_en} against a soft natural sky, crisp realistic details",
            },
            {
                "title": "中景人文與空間律動橫移",
                "scene_desc_zh": f"紀錄片中景緩慢橫移，捕捉「{topic}」現場自然流動的光影與真實環境氛圍，淺景深層次分明。",
                "voiceover_zh": "在流動的光影之間，每一處角落都蘊藏著獨特的節奏與生命力。",
                "prompt_en": f"Cinematic medium lateral dolly shot capturing the authentic atmosphere and subtle natural motion within {topic_en}, shallow depth of field, BBC documentary cinematography",
            },
            {
                "title": "細節紋理與光影特寫",
                "scene_desc_zh": f"極具質感的近景特寫鏡頭，聚焦於「{topic}」獨特的材質肌理與斜射陽光描繪出的立體輪廓。",
                "voiceover_zh": "拉近視線細看，時間在細膩的紋理上留下了獨一無二的印記。",
                "prompt_en": f"Intimate cinematic close-up tracking shot highlighting intricate surface textures, craftsmanship, and warm golden light grazing across details of {topic_en}, shallow depth of field",
            },
            {
                "title": "縱深空間推進與光束氛圍",
                "scene_desc_zh": f"鏡頭平穩穿行於「{topic}」最具空間縱深的區域，柔和自然光束灑落，營造寧靜而莊重的沉浸感。",
                "voiceover_zh": "走進深處，靜謐的光束靜靜流淌，讓人不自覺放慢呼吸。",
                "prompt_en": f"Serene slow forward dolly shot moving through an atmospheric space of {topic_en}, volumetric warm sunbeams illuminating the scene, rich contrast and natural color grading",
            },
            {
                "title": "黃金時刻高處開闊遠眺",
                "scene_desc_zh": f"午後黃金時刻，鏡頭沿著高處視野緩慢滑動，俯瞰「{topic}」與遠方綿延展開的壯麗全景。",
                "voiceover_zh": "站在高處回望，開闊的視野將整片風景盡收眼底。",
                "prompt_en": f"Sweeping panoramic gliding shot from an elevated vantage point overlooking {topic_en} and the surrounding horizon during late afternoon golden hour, warm long shadows",
            },
            {
                "title": "夕陽餘暉緩慢拉遠收尾",
                "scene_desc_zh": f"日落暮色中，航拍鏡頭從「{topic}」緩慢向後拉遠並微升，溫暖橘金晚霞為整段旅程優雅收尾。",
                "voiceover_zh": "伴隨著溫柔的夕陽餘暉，這場難忘的探索也在暮色中畫下完美句點。",
                "prompt_en": f"Epic cinematic aerial pull-back shot slowly ascending away from {topic_en} at sunset golden hour, warm amber and rose twilight sky, peaceful majestic documentary ending",
            },
        ]

    # 依所需鏡數截取或延伸（8 鏡池縮成 4 鏡時取首/進/中/尾代表鏡；原生 4 幕池則直接依序全取）
    selected_raw: List[Dict[str, str]] = []
    if shot_cnt == 4 and len(base_pool) >= 8:
        indices = [0, 1, 4, 7]
        selected_raw = [base_pool[i] for i in indices]
    elif shot_cnt <= len(base_pool):
        selected_raw = base_pool[:shot_cnt]
    else:
        for i in range(shot_cnt):
            item = dict(base_pool[i % len(base_pool)])
            if i >= len(base_pool):
                item["title"] = f"{item['title']}（視角延伸 {i - len(base_pool) + 1}）"
            selected_raw.append(item)

    shots: List[Dict[str, Any]] = []
    # 45 秒採用 MiniMax H3 四幕首尾幀接力黃金節奏：首幕 15 秒 + 後三幕各 10 秒
    relay_45_durs = [15.0, 10.0, 10.0, 10.0]
    for idx, s in enumerate(selected_raw, start=1):
        shot_dur = relay_45_durs[idx - 1] if (target_duration == 45 and len(selected_raw) == 4) else per_shot_sec
        shots.append(
            {
                "shot_order": idx,
                "title": s["title"],
                "duration_sec": shot_dur,
                "scene_desc_zh": s["scene_desc_zh"],
                "voiceover_zh": s["voiceover_zh"],
                "prompt_en": ensure_full_english_prompt(
                    s["prompt_en"], aspect_ratio=aspect_ratio, visual_style=visual_style
                ),
            }
        )

    return style_lock, DEFAULT_NEGATIVE_PROMPT, shots


# -------------------------
# 免 Key 貼上解析器（支援 JSON 與 Markdown 文字格式）
# -------------------------
def parse_pasted_storyboard(
    raw_text: str,
    aspect_ratio: str = "16:9",
    visual_style: str = "35mm_doc",
    default_duration_sec: float = 8.0,
) -> Tuple[str, str, List[Dict[str, Any]]]:
    """
    將使用者從 Gemini 網頁版貼回的內容（或測試腳本輸入）解析為標準分鏡結構。
    支援：
    1. 純 JSON 或 ```json ... ``` 區塊（物件或陣列）
    2. Markdown 分鏡格式（例如 `### Shot 01 ...`、`- 畫面內容：...`、英文 Prompt 區塊）
    3. 多行純英文/中英混合清單
    """
    text = (raw_text or "").strip()
    if not text:
        raise ValueError("貼上的內容為空，請貼上分鏡 JSON 或 Markdown 文字。")

    style_cfg = VISUAL_STYLE_CONFIG.get(visual_style, VISUAL_STYLE_CONFIG["35mm_doc"])
    style_lock = style_cfg["style_lock"]
    negative_prompt = DEFAULT_NEGATIVE_PROMPT

    # 1. 嘗試擷取 JSON
    json_candidate = text
    fenced_match = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, re.IGNORECASE)
    if fenced_match:
        possible = fenced_match.group(1).strip()
        if possible.startswith("{") or possible.startswith("["):
            json_candidate = possible
    else:
        # 尋找第一個 { 或 [ 到最後一個 } 或 ]
        brace_match = re.search(r"(\{[\s\S]*\}|\[[\s\S]*\])", text)
        if brace_match:
            json_candidate = brace_match.group(1).strip()

    try:
        parsed = json.loads(json_candidate)
        raw_shots = []
        if isinstance(parsed, dict):
            style_lock = str(parsed.get("style_lock") or style_lock).strip()
            negative_prompt = str(parsed.get("negative_prompt") or negative_prompt).strip()
            raw_shots = parsed.get("shots") or parsed.get("storyboard") or []
        elif isinstance(parsed, list):
            raw_shots = parsed

        if isinstance(raw_shots, list) and len(raw_shots) > 0:
            normalized = []
            for idx, item in enumerate(raw_shots, start=1):
                if isinstance(item, str):
                    normalized.append(
                        {
                            "shot_order": idx,
                            "title": f"鏡頭 {idx:02d}",
                            "duration_sec": default_duration_sec,
                            "scene_desc_zh": item,
                            "voiceover_zh": "",
                            "prompt_en": ensure_full_english_prompt(
                                item, aspect_ratio, visual_style
                            ),
                        }
                    )
                elif isinstance(item, dict):
                    prompt_val = (
                        item.get("prompt_en")
                        or item.get("prompt")
                        or item.get("english_prompt")
                        or item.get("scene_desc_zh")
                        or f"Cinematic documentary shot {idx}"
                    )
                    normalized.append(
                        {
                            "shot_order": int(item.get("shot_order") or idx),
                            "title": str(
                                item.get("title") or item.get("name") or f"鏡頭 {idx:02d}"
                            ).strip(),
                            "duration_sec": float(
                                item.get("duration_sec")
                                or item.get("duration")
                                or default_duration_sec
                            ),
                            "scene_desc_zh": str(
                                item.get("scene_desc_zh")
                                or item.get("description")
                                or item.get("scene")
                                or ""
                            ).strip(),
                            "voiceover_zh": str(
                                item.get("voiceover_zh")
                                or item.get("voiceover")
                                or item.get("narration")
                                or ""
                            ).strip(),
                            "prompt_en": ensure_full_english_prompt(
                                str(prompt_val), aspect_ratio, visual_style
                            ),
                        }
                    )
            if normalized:
                return style_lock, negative_prompt, normalized
    except Exception:
        pass

    # 2. 解析 Markdown 分鏡區塊（例如 ### Shot 01 / 鏡頭 1）
    shot_blocks = re.split(
        r"(?m)^(?:#{1,4}\s*)?(?:Shot\s*\d+|鏡頭\s*\d+|分鏡\s*\d+)[^\n]*$", text
    )
    headers = re.findall(
        r"(?m)^(?:#{1,4}\s*)?((?:Shot\s*\d+|鏡頭\s*\d+|分鏡\s*\d+)[^\n]*)$", text
    )

    if headers and len(shot_blocks) > 1:
        shots_md: List[Dict[str, Any]] = []
        for idx, (hdr, body) in enumerate(zip(headers, shot_blocks[1:]), start=1):
            clean_title = re.sub(
                r"^(?:Shot\s*\d+|鏡頭\s*\d+|分鏡\s*\d+)\s*[｜|:\-—]*\s*(?:\d+:\d+[–\-]\d+:\d+\s*[｜|:\-—]*\s*)?",
                "",
                hdr.strip(),
            ).strip() or f"鏡頭 {idx:02d}"

            desc_match = re.search(
                r"(?:畫面內容|畫面描述|中文說明)[：:]\s*([^\n]+)", body
            )
            vo_match = re.search(
                r"(?:後期配音參考(?:（可選）)?|配音靈感|旁白)[：:]\s*([^\n]+)", body
            )
            code_match = re.search(r"```(?:text|prompt)?\s*([\s\S]*?)```", body)
            en_line_match = re.search(
                r"(?:英文\s*Prompt|Prompt)[：:]\s*([^\n`]+)", body, re.IGNORECASE
            )

            en_prompt = ""
            if code_match:
                en_prompt = code_match.group(1).strip()
            elif en_line_match:
                en_prompt = en_line_match.group(1).strip()
            else:
                # 找尋主要由英文字母組成的長句
                for line in body.splitlines():
                    ln = line.strip(" -*>`")
                    if len(ln) > 30 and sum(c.isascii() and c.isalpha() for c in ln) > len(ln) * 0.5:
                        en_prompt = ln
                        break
            if not en_prompt:
                en_prompt = desc_match.group(1).strip() if desc_match else clean_title

            shots_md.append(
                {
                    "shot_order": idx,
                    "title": clean_title,
                    "duration_sec": default_duration_sec,
                    "scene_desc_zh": desc_match.group(1).strip() if desc_match else clean_title,
                    "voiceover_zh": vo_match.group(1).strip() if vo_match else "",
                    "prompt_en": ensure_full_english_prompt(
                        en_prompt, aspect_ratio, visual_style
                    ),
                }
            )
        if shots_md:
            return style_lock, negative_prompt, shots_md

    # 3. 逐行純文字解析（每段非空行視為一鏡）
    lines = [
        re.sub(r"^\d+[\.\)、\s]+", "", ln.strip())
        for ln in text.splitlines()
        if ln.strip() and not ln.strip().startswith("```")
    ]
    if not lines:
        raise ValueError("無法從貼上的內容解析出有效分鏡，請確認格式後重試。")

    fallback_shots: List[Dict[str, Any]] = []
    for idx, ln in enumerate(lines, start=1):
        # 支援「標題｜中文描述｜英文Prompt」以直線或分號分隔
        parts = [p.strip() for p in re.split(r"[｜|]", ln) if p.strip()]
        if len(parts) >= 3:
            title, desc_zh, en_p = parts[0], parts[1], parts[2]
        elif len(parts) == 2:
            title, desc_zh, en_p = parts[0], parts[0], parts[1]
        else:
            title = f"鏡頭 {idx:02d}"
            desc_zh = ln
            en_p = ln

        fallback_shots.append(
            {
                "shot_order": idx,
                "title": title[:40],
                "duration_sec": default_duration_sec,
                "scene_desc_zh": desc_zh,
                "voiceover_zh": "",
                "prompt_en": ensure_full_english_prompt(en_p, aspect_ratio, visual_style),
            }
        )
    return style_lock, negative_prompt, fallback_shots


# -------------------------
# 外部大腦呼叫：免費 Gemini 文字 Key 與本機 Ollama
# -------------------------
def generate_via_gemini(
    brief: str,
    api_key: str,
    model: str = "gemini-2.5-flash",
    target_duration: int = 60,
    aspect_ratio: str = "16:9",
    visual_style: str = "35mm_doc",
) -> Tuple[str, str, List[Dict[str, Any]]]:
    if not api_key or not api_key.strip():
        raise ValueError("尚未填寫 Gemini API Key。請在右上角「系統設定」填入免費 Key，或切換為「免 Key 模式」。")

    prompt = build_master_prompt_for_web(brief, target_duration, aspect_ratio, visual_style)
    model_name = (model or "gemini-2.5-flash").strip()
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent"

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0.6,
            "responseMimeType": "application/json",
        },
    }
    with httpx.Client(timeout=45.0) as client:
        resp = client.post(url, params={"key": api_key.strip()}, json=payload)
        if resp.status_code != 200:
            raise RuntimeError(f"Gemini API 回傳錯誤 (HTTP {resp.status_code})：{resp.text[:200]}")
        data = resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini API 未回傳任何內容，請稍後再試。")
    parts = candidates[0].get("content", {}).get("parts", [])
    text_out = "".join(p.get("text", "") for p in parts)
    shot_cnt = recommended_shot_count(target_duration)
    per_sec = round((target_duration + (shot_cnt - 1) * 0.5) / shot_cnt, 1)
    return parse_pasted_storyboard(text_out, aspect_ratio, visual_style, per_sec)


def generate_via_ollama(
    brief: str,
    base_url: str = "http://127.0.0.1:11434",
    model: str = "qwen2.5:7b",
    target_duration: int = 60,
    aspect_ratio: str = "16:9",
    visual_style: str = "35mm_doc",
) -> Tuple[str, str, List[Dict[str, Any]]]:
    prompt = build_master_prompt_for_web(brief, target_duration, aspect_ratio, visual_style)
    clean_base = (base_url or "http://127.0.0.1:11434").rstrip("/")
    url = f"{clean_base}/api/generate"
    payload = {
        "model": (model or "qwen2.5:7b").strip(),
        "prompt": prompt,
        "format": "json",
        "stream": False,
    }
    try:
        with httpx.Client(timeout=90.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code != 200:
                raise RuntimeError(f"Ollama 回傳錯誤 (HTTP {resp.status_code})：{resp.text[:200]}")
            data = resp.json()
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"無法連線至本機 Ollama ({clean_base})，請確認已啟動 Ollama 服務，或改用「免 Key 模式」。"
        ) from exc

    text_out = data.get("response", "")
    shot_cnt = recommended_shot_count(target_duration)
    per_sec = round((target_duration + (shot_cnt - 1) * 0.5) / shot_cnt, 1)
    return parse_pasted_storyboard(text_out, aspect_ratio, visual_style, per_sec)
