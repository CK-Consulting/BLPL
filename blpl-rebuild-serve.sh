#!/bin/bash
readenv() {
  local filePath="${HOME}/projects/blpl/.env"

  if [ ! -f "$filePath" ]; then
    # silently be done
    # put some error / echo if you prefer non-silent errors
    return 0
  fi

#  echo "Reading $filePath"
  while read -r line; do
    if [[ "$line" =~ ^\s*#.*$ || -z "$line" ]]; then
      continue
    fi

     # Split the line into key and value. Trim whitespace on either side.
    key=$(echo "$line" | cut -d '=' -f 1 | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    value=$(echo "$line" | cut -d '=' -f 2- | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')

    # Leaving the below here... normally this works, but if you have something like
    # FOO="  string with leading and trailing  "
    # then the leading / trailing spaces are deleted. FOO="a word", FOO='a word', and FOO=a word all generally work
    # so leave the quotes
    # Remove single quotes, double quotes, and leading/trailing spaces from the value
    # value=$(echo "$value" | sed -e "s/^'//" -e "s/'$//" -e 's/^"//' -e 's/"$//' -e 's/^[[:space:]]*//;s/[[:space:]]*$//')

    # Export the key and value as environment variables
    # echo "$key=$value"
    export "$key=$value"

  done < "$filePath"
}

_evalBg() {
    eval "$@" &>/dev/null & disown;
}
source ${HOME}/projects/blpl/.venv/bin/activate
uv pip install -e ".[webapp]"              # fastapi + uvicorn backend
cd ui && npm install && npm run build
readenv      # SolidJS frontend → blpl/webapp/static/
cmd="blpl serve"
_evalBg "${cmd}";

