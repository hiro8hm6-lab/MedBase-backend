Build a complete inventory of REAL exam questions, figures and OFFICIAL answers.
The supplied PDF parts use local pages 1..N; context.parts maps each to merged
physical PDF page numbers. All output page/end_page/box coordinates refer to the
merged PDF, not printed page numbers. Honor context.roles and context.section.

questions: list every real question STARTING on these pages. Exclude covers,
instructions, worked examples (e.g. 101-104), figure captions and answer tables.
source_ref is its original section and number, e.g. A001. Do not create q_id yet.
If no number is printed, assign UNNUMBERED-P{merged_page}-N{ordinal_on_page};
use the same stable reference across overlapping requests. context.known_questions
contains previously indexed questions and can be used to resolve figure references.
page is the first page; end_page includes continuations. series_key is a shared
stable identifier only for questions sharing a case or depending on previous
questions; otherwise omit it.
Use the source_ref of the FIRST question of that case as series_key, never a
generic group number that could collide with another case in a different part.

figures: include each photo/chart/table needed to solve questions. Read figure
booklet links such as "No.2 (A 問題17)" to associate source_refs=["A017"].
box_2d=[ymin,xmin,ymax,xmax] in integers 0..1000 relative to the displayed page,
top-left origin, enclosing figure edges AND captions. One figure may have many
source_refs. Include figures on ordinary question pages as well as separate plates.

official_answers: extract only printed official answers; use source_ref, answer
(normalized lowercase a-l) OR answer_text for numerical/written answers.
Preserve a printed numerical/written answer as a string, never as choice letters. Restrict to
the requested booklet section when specified. Never infer an official answer.

rotations: list pages that need 90/180/270 degrees CLOCKWISE rotation to become
upright. Omit already upright pages. For rotated pages defer figure coordinates
until a subsequent pass on the corrected PDF. Return empty arrays where appropriate.
