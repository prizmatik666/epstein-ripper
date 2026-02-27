these are index/scan tools - beyond the epstein_ripper.py 's scan/indexer. The ripper's scan utility is generally adequate for building the download index - i do recommend setting the ripper's code value for page / streak before ending scan from 6 to a higher value (i used 300, and rightfully so, on data9 to make sure i found everything) because DOJ's site is terrible with how it serves new pages/file-names .

to use- put these tools in the main epstein directory (or whatever your staging directory is that contains the tools and various data#/ directories)

db_index.py -> uses sqlite to scan and make a db of .pdf's from the doj site. MUCH faster than the ripper's scan that checksj filenames vs the .json every page/files found , but as mentioned at end of this readme, have had better results on some datasets using the ripper vs the db.
[ though currently - scanning dataset 10 - not having issues yet like i was on data9 with db_index .] 
[ data9 was returning same pdf list around page 1130 or so, wouldnt break (with db)
but- data9 ,though encountering high streak's (over 200) would break out and find new files eventually, before getting a 300page streak and completing with the rippers .json indexer. so full completeness can't be confirmed,,,with anything- which i believe the DOJ wants/happy side effect ]]

db_to_json.py -> converts a db_index scan file into a .json that's useable with the ripper for downloads. (after renaming to index_data#.json and ensuring its the only one in that data# directory, be sure to backup any previous .json's you dont want to lose)

dupe_check.py -> scans .json files to make sure theres no duplicate entries

dupe_index.py -> duplicates a .json index, converts all downloaded values to false. useful for making copies of .json index for sharing, backup, etc.

index_repair.py -> should be included here, but i've included that in the main directory of the repo as it's more useful to the main functionality than these 'side utils' are. These are more for experimentation/ offering users another scanning method to experiment with. 

again- for dataset 9 - the ripper's scan returned me over 200k+ filenames. the DB stopped returning new filenames at around 32k+ range. 

i havent found a concrete reasoning for this- but it's led me to go with the ripper for building accurate scanning index files, but the db can be useful for some applications.

thought it wise to include them now in a side directory to make updating repo easier down the road if i further refine them or add new tools. 