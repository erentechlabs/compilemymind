#!/bin/bash

# Image optimization script for Hugo
# This will convert PNG images to WebP format for better performance

echo "🖼️  Starting image optimization..."

# Check if cwebp is installed
if ! command -v cwebp &> /dev/null; then
    echo "⚠️  cwebp not found. Installing..."
    if [[ "$OSTYPE" == "darwin"* ]]; then
        brew install webp
    else
        echo "Please install webp tools: sudo apt-get install webp"
        exit 1
    fi
fi

# Find all PNG and JPG files in content and static directories
find content static -type f \( -iname "*.png" -o -iname "*.jpg" -o -iname "*.jpeg" \) | while read img; do
    # Skip if WebP already exists
    webp_file="${img%.*}.webp"
    if [ ! -f "$webp_file" ]; then
        echo "Converting: $img"
        cwebp -q 80 "$img" -o "$webp_file"
    fi
done

echo "✅ Image optimization complete!"
echo "💡 Tip: Hugo will automatically serve WebP images to supported browsers"
