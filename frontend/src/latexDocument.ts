// Keep the format boundary aligned with backend/latex_content.py.
const spaceOrComment = '(?:\\s|%[^\\r\\n]*(?:\\r\\n?|\\n|$))*'
const options = `(?:\\[[^\\[\\]]*\\]${spaceOrComment})?`
const argument = `\\{[^{}]+\\}${spaceOrComment}`
const preclass = `(?:\\\\RequirePackage${spaceOrComment}${options}${argument}${options}|\\\\PassOptionsTo(?:Package|Class)${spaceOrComment}${argument}${argument})`
const documentStart = new RegExp(`^${spaceOrComment}(?:${preclass})*\\\\documentclass${spaceOrComment}${options}\\{[^{}]+\\}`)

export function isLatexDocument(source: string): boolean {
  return documentStart.test(source)
}
