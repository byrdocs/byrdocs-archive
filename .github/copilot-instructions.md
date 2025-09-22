# GitHub Copilot Instructions for Metadata Pull Request Review

## General Guidelines

When reviewing pull requests that add or modify metadata files in this repository, you must:

1. **Always respond and provide reviews in Chinese**
2. **Thoroughly check all aspects** described in these instructions
3. **Reference specific documentation** when issues are found

## Metadata File Requirements

### File Structure and Naming
- Metadata files must be YAML format (.yml extension)
- Files must be named using the MD5 sum of the corresponding resource file
- Files must be placed in the `/metadata` directory in the repository root
- Each metadata file must correspond to exactly one resource file

### Schema Validation
Every YAML file must begin with the appropriate schema comment:
- Books: `# yaml-language-server: $schema=https://byrdocs.org/schema/book.yaml`
- Tests: `# yaml-language-server: $schema=https://byrdocs.org/schema/test.yaml` 
- Documents: `# yaml-language-server: $schema=https://byrdocs.org/schema/doc.yaml`

## File Type Classification

Check that files are correctly classified into one of three types:

### 1. Books (book)
**Must meet ALL criteria:**
- Is an actual book (not lecture notes, courseware, etc.)
- PDF format electronic book only
- Formally published work (publicly released by individual/organization with proper publication)
- Educational resource (novels, essays, comics excluded unless proven educational)

**Required fields for books:**
- `id`: MD5 hash of the file
- `url`: BYR Docs URL (format: https://byrdocs.org/files/{md5}.pdf)
- `type`: Must be exactly "book"
- `data.title`: Must match original book title exactly
  - Chinese books (including translated foreign books): Use Chinese title
  - Foreign books: Use foreign title, with exceptions:
    - Language course textbooks: Use Chinese title
    - International college textbooks authored by Chinese: Use bilingual title
    - Traditional Chinese titles: Treat as foreign titles
- `data.authors`: Array format, use original names preferred over translations
- `data.translators`: Optional, only if translators exist
- `data.edition`: Optional, for translations use translated version number
- `data.publisher`: Optional, for translations use translation publisher
- `data.publish_year`: Optional, for translations use translation year
- `data.isbn`: Required, must be ISBN13 format array
- `data.filetype`: Must be exactly "pdf"

### 2. Tests (test) 
**Must meet ALL criteria:**
- PDF format electronic material
- From Beijing University of Posts and Telecommunications only
- Midterm or final examination papers only (not monthly tests, homework, etc.)
- Must be actual exam papers that were given (not question banks or mock tests)

**Required fields for tests:**
- `id`: MD5 hash of the file
- `url`: BYR Docs URL (format: https://byrdocs.org/files/{md5}.pdf)
- `type`: Must be exactly "test"
- `data.college`: Optional array, only fill if confirmed this college actually took this exam
- `data.course`: Required object with:
  - `type`: Either "本科" or "研究生" (optional if unknown)
  - `name`: Required, full course name (no abbreviations, include all letters/parentheses)
- `data.time`: Required object with:
  - `start`: Required academic year start
  - `end`: Required academic year end  
  - `semester`: Optional, either "First" or "Second"
  - `stage`: Optional, either "期中" or "期末"
- `data.filetype`: Must be "pdf" or "wiki"
- `data.content`: Required array, must contain "原题" and/or "答案"

### 3. Documents (doc)
**Must meet ALL criteria:**
- PDF or ZIP format
- Educational resource
- Must correspond to at least one course
- Must be related to course study or exam preparation (not competition materials)

**Required fields for documents:**
- `id`: MD5 hash of the file
- `url`: BYR Docs Publish URL
- `type`: Must be exactly "doc"
- `data.title`: Required, self-summarized appropriate title
- `data.filetype`: Must be "pdf" or "zip"
- `data.course`: Required non-empty array of objects with:
  - `type`: Either "本科" or "研究生" (optional if unknown)
  - `name`: Required full course name
- `data.content`: Required non-empty array, must contain one or more of:
  - "思维导图" (mind maps)
  - "题库" (question banks not fitting book/test categories)
  - "答案" (answers to homework, exercises, etc.)
  - "知识点" (study aids, review materials)
  - "课件" (courseware, handouts, teaching materials)

## Quality Requirements

### File Quality Issues to Check:
1. **Clarity**: Files must be clear and readable
2. **Completeness**: No missing pages, duplicate pages, or page order issues
3. **Organization**: No mixed content (multiple test sets in one PDF)
4. **Cover**: PDF files must have appropriate cover pages

### Deduplication Principles:

**For Books:**
- Higher clarity preferred
- Files with covers preferred
- Files with complete bookmarks preferred

**For Tests:**
- Separate question and answer files should be merged
- Questions-only + embedded-answers should be combined (questions first, answers after)
- Answer-only files are redundant if complete question+answer file exists
- Same year/subject with different content may be legitimate (makeup exams, different colleges, multiple exam versions)

## Review Process

When reviewing metadata PRs:

1. **Verify file naming**: Check MD5 sum matches filename
2. **Validate YAML structure**: Ensure proper formatting and required fields
3. **Check schema compliance**: Verify correct schema header and field types
4. **Validate classification**: Confirm file type matches content and meets criteria  
5. **Review field accuracy**: Check all required fields are present and correctly formatted
6. **Assess quality**: Consider if file meets quality standards
7. **Check for duplicates**: Identify potential duplicate content

## Response Requirements

When providing feedback:

### For Compliant Submissions:
- Thanks to the contributors for their contributions
- Approve the submission

Note: Do not provide redundant content, do not restate the guidelines and rules, and keep the comments concise.

### For Non-Compliant Submissions:
- Clearly explain specific issues in Chinese
- Reference the exact requirements that were not met
- Direct contributors to review:
  - Contributing Guide: https://github.com/byrdocs/byrdocs-archive/blob/master/CONTRIBUTING.md
  - File Rules: https://github.com/byrdocs/byrdocs-archive/blob/master/docs/%E6%96%87%E4%BB%B6%E8%A7%84%E5%88%99.md
  - Metadata Rules: https://github.com/byrdocs/byrdocs-archive/blob/master/docs/%E5%85%83%E4%BF%A1%E6%81%AF%E8%A7%84%E5%88%99.md
- Provide specific guidance on how to fix the issues
- Request changes before approval

## Special Cases

### Multi-College Tests:
- Only include colleges in array if confirmed they took this specific exam
- When uncertain, omit college field rather than guess

### Translation Books:
- Use translation publisher, year, edition, and ISBN
- Include original version information in title if relevant

Remember: The goal is to maintain high-quality, consistent metadata that enables effective search and organization of educational resources. Be thorough but constructive in reviews.
