# Generated title hotfix acceptance cases

The title cleaner must return only the readable title for all of these AI response shapes:

- `{"title":"Oops, Milky Matt! Try, Try Again!"}` → `Oops, Milky Matt! Try, Try Again!`
- `Title: Oops, Milky Matt! Try, Try Again!` → `Oops, Milky Matt! Try, Try Again!`
- `**Oops, Milky Matt! Try, Try Again!**` → `Oops, Milky Matt! Try, Try Again!`
- quoted title → unwrapped title
- fenced JSON containing a `title` key → title value only

Normal internal punctuation such as commas, apostrophes, question marks and exclamation marks must be preserved.
