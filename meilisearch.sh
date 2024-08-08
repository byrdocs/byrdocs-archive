#!/bin/bash

# Usage: ./meilisearch_update.sh <metadata_file_path> <meilisearch_api_key> <byrdocs_token>

METADATA_FILE=$1
MEILISEARCH_API_KEY=$2
BYRDOCS_TOKEN=$3
API_URL="https://byrdocs.org/api/indexes/docs/documents?primaryKey=id"
TASK_STATUS_URL="https://byrdocs.org/api/tasks"


# Initial request to update documents
RESPONSE=$(curl --location --silent --show-error \
    --header "Content-Type: application/json" \
    --header "Authorization: Bearer $MEILISEARCH_API_KEY" \
    --header "X-Byrdocs-Token: $BYRDOCS_TOKEN" \
    --data "@$METADATA_FILE" \
    $API_URL)

echo "Response: $RESPONSE"

# Extract taskUid from the response
TASK_UID=$(echo $RESPONSE | jq -r '.taskUid')

# Function to get task details
get_task_details() {
    curl --location --silent --show-error \
    --header "Authorization: Bearer $MEILISEARCH_API_KEY" \
    --header "X-Byrdocs-Token: $BYRDOCS_TOKEN" \
    "$TASK_STATUS_URL/$TASK_UID"
}

# Check task status until it is succeeded or times out
TIMEOUT=60
INTERVAL=5
ELAPSED=0


while [ $ELAPSED -lt $TIMEOUT ]; do
    TASK_DETAILS=$(get_task_details)
    echo "Task details: $TASK_DETAILS"
    STATUS=$(echo $TASK_DETAILS | jq -r '.status')
    echo "Task status: $STATUS (Elapsed time: ${ELAPSED}s)"

    if [ "$STATUS" = "succeeded" ]; then
        RECEIVED_DOCUMENTS=$(echo $TASK_DETAILS | jq -r '.details.receivedDocuments')
        INDEXED_DOCUMENTS=$(echo $TASK_DETAILS | jq -r '.details.indexedDocuments')
        echo "Task completed successfully."
        echo "Received Documents: $RECEIVED_DOCUMENTS"
        echo "Indexed Documents: $INDEXED_DOCUMENTS"
        exit 0
    elif [ "$STATUS" != "enqueued" ] && [ "$STATUS" != "processing" ]; then
        echo "Error: Task failed with status $STATUS."
        exit 1
    fi

    sleep $INTERVAL
    ELAPSED=$((ELAPSED + INTERVAL))
done

echo "Error: Task did not complete within the timeout period."
exit 1
